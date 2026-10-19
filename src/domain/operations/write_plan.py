# SPDX-License-Identifier: MIT-0
"""Pure ADR-075 §3 plan-intent/plan authority: closed forms, identities, and schedules.

Everything here is deterministic: the closed ``SUCCESSOR_OWNER``, intent ``META``,
intent ``UNIT#``, plan ``META``, and plan ``UNIT#`` item forms; the pinned
``stage1-plan-intent-roster/v1`` / ``stage1-plan-intent/v1`` /
``stage1-source-delivery/v1`` / ``stage1-plan-digest/v1`` /
``stage1-plan-attempt-budget/v1`` / ``stage1-plan-materialization/v1`` identity
constructors over ``canonical_authority_digest``; the sparse ready-GSI key forms; and
the capped-backoff retry schedule. Constructors encode exactly and never validate
member forms — the §3 committed vectors deliberately use synthetic members — while
the closed dataclasses validate production forms. The adapter owns DynamoDB; this
module owns every byte-level decision.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from domain.operations.status import canonical_authority_bytes, canonical_authority_digest

WRITE_PLAN_PREFIX: Final = "WRITE_PLAN#"
SUCCESSOR_OWNER_RECORD_KEY: Final = "SUCCESSOR_OWNER"
META_RECORD_KEY: Final = "META"
UNIT_RECORD_PREFIX: Final = "UNIT#"
INTENT_SEGMENT: Final = "#INTENT#"
READY_STATUS_VALUE: Final = "ready"

ROSTER_NAMESPACE: Final = "stage1-plan-intent-roster/v1"
INTENT_NAMESPACE: Final = "stage1-plan-intent/v1"
SOURCE_DELIVERY_NAMESPACE: Final = "stage1-source-delivery/v1"
PLAN_DIGEST_NAMESPACE: Final = "stage1-plan-digest/v1"
PLAN_ATTEMPT_BUDGET_NAMESPACE: Final = "stage1-plan-attempt-budget/v1"
PLAN_MATERIALIZATION_NAMESPACE: Final = "stage1-plan-materialization/v1"

MAX_INTENT_UNITS: Final = 15
MAX_OBJECTS_PER_UNIT: Final = 37
MAX_PLAN_ATTEMPTS: Final = 32
MAX_ENCODED_IDENTITY_BYTES: Final = 128
# §11 ``maxPartitionIdentityBytes``: the Publication-State-owned ``PartitionIdentity.identity``
# carried as the roster ``partition_key-or-null`` member gets its own reviewed bound rather
# than the internal-identity cap a realistic ~150-byte identity would always exceed.
MAX_PARTITION_IDENTITY_BYTES: Final = 256
CHILD_OUTCOME_REDUCTION_VERSION: Final = 2
COMPLETE_PLAN_ACTION_CAP: Final = 51
ORDINARY_PERSISTENCE_ACTION_CAP: Final = 20
PLANNED_REPLACEMENT_ACTION_CAP: Final = 100

_BASE_BACKOFF_SECONDS: Final = 60
_MAX_BACKOFF_SECONDS: Final = 900
_BACKOFF_EXPONENT_CAP: Final = 4
_MICROS_PER_SECOND: Final = 1_000_000
_INSTANT_DIGITS: Final = 20

_INSTANT_TEXT: Final = re.compile(r"\A[0-9]{20}\Z")
_DIGEST_TEXT: Final = re.compile(r"\A[0-9a-f]{64}\Z")


class WritePlanError(ValueError):
    """A plan-intent or plan item, key, or identity violates the closed §3 contract."""


class IntentState(StrEnum):
    """Closed intent state vocabulary; no blocked or refused state exists."""

    PENDING = "pending"
    CLAIMED = "claimed"
    ADMITTED = "admitted"


class IntentOriginKind(StrEnum):
    """Closed origin vocabulary for the intent authority tuple."""

    SOURCE_DELIVERY = "source_delivery"
    PLAN_ATTEMPT_BUDGET = "plan_attempt_budget"


class PlanStatus(StrEnum):
    """Closed plan ``META`` status vocabulary; §8 names the three terminal states."""

    PLANNED = "planned"
    COMPLETED = "completed"
    SUPERSEDED = "superseded"
    ADMISSION_FAILED = "admission_failed"


class PlanUnitStatus(StrEnum):
    """Closed plan unit execution states; this slice creates only ``queued``.

    ``superseded`` is written only by the reviewed orphan disposition
    (``adapters.jobs.supersede_orphan_units``, TD-50), always with its recorded evidence digest
    in ``UNIT_SUPERSESSION_EVIDENCE_FIELD``; no writer path produces it.
    """

    QUEUED = "queued"
    WRITING = "writing"
    ATTESTED = "attested"
    FAILED = "failed"
    SUPERSEDED = "superseded"


UNIT_SUPERSESSION_EVIDENCE_FIELD: Final = "supersession_evidence_digest"
"""The unit (and plan ``META``) attribute holding the disposition's per-record evidence digest."""


class PlanUnitAdmission(StrEnum):
    """Closed per-unit queue-admission axis, separate from execution status."""

    PENDING = "pending"
    ADMITTED = "admitted"


# --- Shared validators ----------------------------------------------------------------------------


def is_canonical_instant(value: str) -> bool:
    """Whether text is a canonical 20-digit UTC epoch-microseconds instant."""
    return bool(_INSTANT_TEXT.match(value))


def is_canonical_digest(value: str) -> bool:
    """Whether text is 64 lowercase hex characters."""
    return bool(_DIGEST_TEXT.match(value))


def _require_instant(value: str, name: str) -> None:
    if not is_canonical_instant(value):
        raise WritePlanError(f"{name} must be a 20-digit epoch-microseconds instant.")


def _require_digest(value: str, name: str) -> None:
    if not is_canonical_digest(value):
        raise WritePlanError(f"{name} must be 64 lowercase hex characters.")


def _require_identity(value: str, name: str) -> None:
    if not value.strip():
        raise WritePlanError(f"{name} must be nonblank text.")
    if len(value.encode("utf-8")) > MAX_ENCODED_IDENTITY_BYTES:
        raise WritePlanError(f"{name} exceeds the 128-byte encoded identity cap.")


def _require_partition_identity(value: str, name: str) -> None:
    if not value.strip():
        raise WritePlanError(f"{name} must be nonblank text.")
    if len(value.encode("utf-8")) > MAX_PARTITION_IDENTITY_BYTES:
        raise WritePlanError(f"{name} exceeds the 256-byte partition identity cap.")


def _is_plain_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# --- Canonical keys -------------------------------------------------------------------------------


def owner_partition_key(plan_digest: str) -> str:
    """Return ``WRITE_PLAN#<plan_digest>`` for the one successor owner."""
    _require_digest(plan_digest, "plan digest")
    return f"{WRITE_PLAN_PREFIX}{plan_digest}"


def intent_partition_key(plan_digest: str, intent_id: str) -> str:
    """Return ``WRITE_PLAN#<plan_digest>#INTENT#<intent_id>``."""
    _require_digest(plan_digest, "plan digest")
    _require_digest(intent_id, "intent id")
    return f"{WRITE_PLAN_PREFIX}{plan_digest}{INTENT_SEGMENT}{intent_id}"


def plan_partition_key(plan_digest: str, internal_execution_id: str) -> str:
    """Return the normal plan key ``WRITE_PLAN#<plan_digest>#<internal_execution_id>``."""
    _require_digest(plan_digest, "plan digest")
    _require_digest(internal_execution_id, "internal execution id")
    return f"{WRITE_PLAN_PREFIX}{plan_digest}#{internal_execution_id}"


def unit_record_key(unit_key: str) -> str:
    """Return ``UNIT#<unit_key>`` for intent and plan unit rows alike."""
    _require_identity(unit_key, "unit key")
    return f"{UNIT_RECORD_PREFIX}{unit_key}"


def ready_order_key(due_at: str, intent_id: str) -> str:
    """Render the pending GSI sort key ``<20-digit due epoch-micros>#<intent_id>``."""
    _require_instant(due_at, "due_at")
    _require_digest(intent_id, "intent id")
    return f"{due_at}#{intent_id}"


WRITE_UNIT_MESSAGE_SCHEMA_VERSION: Final = "1"
WRITE_UNIT_MESSAGE_NAMESPACE: Final = "stage1-write-unit-message/v1"


@dataclass(frozen=True, slots=True)
class WriteUnitMessage:
    """The §4 FIFO unit message: exact body bytes, deduplication id, and group id.

    The one pure constructor behind ``unit_message``; phase-1 feasibility preflight encodes it
    without sending, and ``PlanIntentReconciler`` alone passes it to SQS and ``AdmitUnitRequest``.
    """

    body: str
    deduplication_id: str
    group_id: str


@dataclass(frozen=True, slots=True)
class WriteUnitMessageRequest:
    """The six internal owner fields the §4 body carries; no public field exists before claim."""

    plan_key: str
    plan_digest: str
    origin_intent_id: str
    generation: int
    unit_key: str

    def __post_init__(self) -> None:
        # The plan key is a two-digest partition key (140 bytes), not a 128-byte internal identity.
        if not self.plan_key.startswith(WRITE_PLAN_PREFIX) or not self.plan_key.strip():
            raise WritePlanError("message plan key must be a WRITE_PLAN# partition key.")
        _require_digest(self.plan_digest, "plan digest")
        _require_digest(self.origin_intent_id, "origin intent id")
        if not _is_plain_int(self.generation) or self.generation < 0:
            raise WritePlanError("message generation must be a nonnegative integer.")
        _require_identity(self.unit_key, "unit key")


def unit_message(request: WriteUnitMessageRequest) -> WriteUnitMessage:
    """Derive the §4 body, ``MessageDeduplicationId``, and ``MessageGroupId`` byte-exactly."""
    body = canonical_authority_bytes(
        {
            "schema_version": WRITE_UNIT_MESSAGE_SCHEMA_VERSION,
            "plan_key": request.plan_key,
            "plan_digest": request.plan_digest,
            "origin_intent_id": request.origin_intent_id,
            "generation": request.generation,
            "unit_key": request.unit_key,
        }
    ).decode("utf-8")
    return WriteUnitMessage(
        body=body,
        deduplication_id=canonical_authority_digest(
            [WRITE_UNIT_MESSAGE_NAMESPACE, request.plan_key, request.unit_key]
        ),
        group_id=request.unit_key,
    )


# --- Retry schedule -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlanIntentRetrySchedule:
    """One derived claimed→pending schedule: post-increment count and canonical due."""

    retry_count: int
    due_at: str


def plan_intent_retry_schedule(
    pre_increment_retry_count: int, captured_instant: str
) -> PlanIntentRetrySchedule:
    """Derive ``min(60 * 2^min(c, 4), 900)`` seconds of delay from the pre-increment count.

    Integer arithmetic only; the rendered due must fit the 20-digit canonical form or
    the schedule refuses rather than wrapping.
    """
    if not _is_plain_int(pre_increment_retry_count) or pre_increment_retry_count < 0:
        raise WritePlanError("retry count must be a nonnegative integer.")
    _require_instant(captured_instant, "captured instant")
    exponent = min(pre_increment_retry_count, _BACKOFF_EXPONENT_CAP)
    delay_seconds = min(_BASE_BACKOFF_SECONDS * 2**exponent, _MAX_BACKOFF_SECONDS)
    due = int(captured_instant) + delay_seconds * _MICROS_PER_SECOND
    rendered = f"{due:020d}"
    if len(rendered) != _INSTANT_DIGITS:
        raise WritePlanError("due instant overflows the 20-digit canonical form.")
    return PlanIntentRetrySchedule(retry_count=pre_increment_retry_count + 1, due_at=rendered)


# --- Canonical roster entries ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CanonicalObjectEntry:
    """One canonical intent object tuple member in §6 canonical object order."""

    kind: str
    reference: str
    content_checksum: str
    publication_id: str
    partition_key: str | None
    fencing_token: int

    def __post_init__(self) -> None:
        _require_identity(self.kind, "object kind")
        _require_identity(self.reference, "object reference")
        _require_digest(self.content_checksum, "content checksum")
        _require_identity(self.publication_id, "publication id")
        if self.partition_key is not None:
            _require_partition_identity(self.partition_key, "object partition key")
        if not _is_plain_int(self.fencing_token) or self.fencing_token < 1:
            raise WritePlanError("fencing token must be a positive integer.")

    def to_member(self) -> list[object]:
        """Render the exact six-member canonical object list."""
        return [
            self.kind,
            self.reference,
            self.content_checksum,
            self.publication_id,
            self.partition_key,
            self.fencing_token,
        ]

    @property
    def object_key(self) -> tuple[bytes, bytes]:
        """The §6 comparison key: unsigned UTF-8 bytes of ``(kind, reference)``."""
        return (self.kind.encode("utf-8"), self.reference.encode("utf-8"))


@dataclass(frozen=True, slots=True)
class CanonicalUnitEntry:
    """One canonical intent unit entry with its ordered complete object tuple list."""

    unit_key: str
    table_kind: str
    merge_count: int
    objects: tuple[CanonicalObjectEntry, ...]

    def __post_init__(self) -> None:
        _require_identity(self.unit_key, "unit key")
        _require_identity(self.table_kind, "table kind")
        if not _is_plain_int(self.merge_count) or self.merge_count < 0:
            raise WritePlanError("merge count must be a nonnegative integer.")
        if not self.objects or len(self.objects) > MAX_OBJECTS_PER_UNIT:
            raise WritePlanError("a unit carries between 1 and 37 object references.")
        keys = [entry.object_key for entry in self.objects]
        if keys != sorted(keys):
            raise WritePlanError("unit objects must be in ascending (kind, reference) byte order.")
        if len(set(keys)) != len(keys):
            raise WritePlanError("a unit may not repeat an object (kind, reference) key.")

    def to_member(self) -> list[object]:
        """Render the exact canonical unit entry list."""
        return [
            self.unit_key,
            self.table_kind,
            self.merge_count,
            [entry.to_member() for entry in self.objects],
        ]


def _require_canonical_roster(units: tuple[CanonicalUnitEntry, ...]) -> None:
    if len(units) > MAX_INTENT_UNITS:
        raise WritePlanError("an intent roster carries at most 15 units.")
    keys = [unit.unit_key.encode("utf-8") for unit in units]
    if keys != sorted(keys):
        raise WritePlanError("roster units must be in ascending unit_key byte order.")
    if len(set(keys)) != len(keys):
        raise WritePlanError("a roster may not repeat a unit key.")


# --- Pinned identity constructors -----------------------------------------------------------------


def intent_roster_tuple(units: tuple[CanonicalUnitEntry, ...]) -> list[object]:
    """Build the sole ``stage1-plan-intent-roster/v1`` tuple over a canonical roster.

    Noncanonical ordering is rejected rather than hashed; the caller supplies the
    already-canonical roster and this constructor refuses drift.
    """
    _require_canonical_roster(units)
    return [ROSTER_NAMESPACE, [unit.to_member() for unit in units]]


def roster_digest(units: tuple[CanonicalUnitEntry, ...]) -> str:
    """Derive the canonical roster digest."""
    return canonical_authority_digest(intent_roster_tuple(units))


@dataclass(frozen=True, slots=True)
class IntentOrigin:
    """The closed origin pair: kind plus exact source identity or failure transition."""

    kind: IntentOriginKind
    member: str

    def __post_init__(self) -> None:
        if not self.member.strip():
            raise WritePlanError("origin member must be nonblank text.")


@dataclass(frozen=True, slots=True)
class IntentAuthorityInputs:
    """The exact ordered non-roster inputs of the ``stage1-plan-intent/v1`` tuple."""

    root_plan_key: str | None
    source_plan_key: str | None
    plan_digest: str
    origin: IntentOrigin
    units: tuple[CanonicalUnitEntry, ...]


def intent_authority_tuple(inputs: IntentAuthorityInputs) -> list[object]:
    """Build the sole ``stage1-plan-intent/v1`` authority tuple."""
    return [
        INTENT_NAMESPACE,
        inputs.root_plan_key,
        inputs.source_plan_key,
        inputs.plan_digest,
        inputs.origin.kind.value,
        inputs.origin.member,
        intent_roster_tuple(inputs.units),
    ]


def intent_identity(inputs: IntentAuthorityInputs) -> str:
    """Derive the sole generation-independent ``intent_id``."""
    return canonical_authority_digest(intent_authority_tuple(inputs))


def source_delivery_identity(manifest_objects: tuple[tuple[str, str], ...]) -> str:
    """Derive ``origin_source_identity`` from the delivery's exact versioned manifests.

    Pairs must already be in ascending unsigned UTF-8 byte order of key then version;
    noncanonical ordering is rejected rather than hashed.
    """
    if not manifest_objects:
        raise WritePlanError("a source delivery names at least one manifest object.")
    encoded = [(key.encode("utf-8"), version.encode("utf-8")) for key, version in manifest_objects]
    if encoded != sorted(encoded):
        raise WritePlanError("manifest pairs must be in ascending (key, version) byte order.")
    if len(set(encoded)) != len(encoded):
        raise WritePlanError("a source delivery may not repeat a manifest (key, version) pair.")
    return canonical_authority_digest(
        [SOURCE_DELIVERY_NAMESPACE, [[key, version] for key, version in manifest_objects]]
    )


def plan_digest_for_source_delivery(origin_source_identity: str) -> str:
    """Derive the sole ``plan_digest`` from one ``origin_source_identity``."""
    return canonical_authority_digest([PLAN_DIGEST_NAMESPACE, origin_source_identity])


def plan_attempt_budget_origin(source_plan_key: str) -> str:
    """Derive the exact source-plan failure-transition origin member."""
    return canonical_authority_digest([PLAN_ATTEMPT_BUDGET_NAMESPACE, source_plan_key])


SETTLED_ATTEMPT_NAMESPACE: Final = "stage1-plan-settled-attempts/v1"
VERIFIED_DIGEST_NAMESPACE: Final = "stage1-unit-verified-digest/v1"


@dataclass(frozen=True, slots=True)
class SettledAttemptReference:
    """One settled accepted attempt's identity beside its settlement authority digest."""

    physical_table_id: str
    attempt_id: str
    usage_authority_digest: str

    def __post_init__(self) -> None:
        _require_identity(self.physical_table_id, "physical table id")
        _require_identity(self.attempt_id, "attempt id")
        _require_digest(self.usage_authority_digest, "usage authority digest")

    def to_member(self) -> list[str]:
        """Render the exact three-member reference list."""
        return [self.physical_table_id, self.attempt_id, self.usage_authority_digest]


def plan_settled_attempt_digest(references: tuple[SettledAttemptReference, ...]) -> str:
    """Derive the plan ``META`` complete settled-attempt digest over the exact sorted set."""
    if not references or len(references) > MAX_PLAN_ATTEMPTS:
        raise WritePlanError("a settled-attempt digest covers between 1 and 32 attempts.")
    keys = [
        (ref.physical_table_id.encode("utf-8"), ref.attempt_id.encode("utf-8"))
        for ref in references
    ]
    if keys != sorted(keys) or len(set(keys)) != len(keys):
        raise WritePlanError("settled-attempt references must be unique and byte-sorted.")
    return canonical_authority_digest(
        [SETTLED_ATTEMPT_NAMESPACE, [ref.to_member() for ref in references]]
    )


def unit_verified_digest(checksums: tuple[str, ...]) -> str:
    """Derive ``verified_digest``: the hash of the unit's sorted immutable checksums."""
    if not checksums:
        raise WritePlanError("a verified digest covers at least one immutable checksum.")
    for checksum in checksums:
        _require_digest(checksum, "content checksum")
    ordered = sorted(checksums)
    if len(set(ordered)) != len(ordered):
        raise WritePlanError("verified checksums must be unique.")
    return canonical_authority_digest([VERIFIED_DIGEST_NAMESPACE, ordered])


def internal_execution_identity(intent_id: str, claimed_generation: int) -> str:
    """Derive the ``stage1-plan-materialization/v1`` internal execution identity."""
    if not _is_plain_int(claimed_generation) or claimed_generation < 0:
        raise WritePlanError("claimed generation must be a nonnegative integer.")
    return canonical_authority_digest(
        [PLAN_MATERIALIZATION_NAMESPACE, intent_id, claimed_generation]
    )


# --- Closed SUCCESSOR_OWNER form ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SuccessorOwner:
    """The complete mutable ``SUCCESSOR_OWNER`` field set; no other field exists."""

    revision: int
    root_plan_key: str | None
    last_plan_key: str | None
    last_generation: int
    active_plan_key: str | None
    active_intent_id: str | None
    reserved_intent_id: str | None
    reserved_intent_digest: str | None
    reserved_generation: int | None

    latest_internal_execution_id: str | None

    def __post_init__(self) -> None:
        if not _is_plain_int(self.revision) or self.revision < 0:
            raise WritePlanError("owner revision must be a nonnegative integer.")
        if (self.last_plan_key is None) != (self.last_generation == -1):
            raise WritePlanError("last_plan_key is absent exactly while last_generation is -1.")
        if (self.root_plan_key is None) != (self.last_plan_key is None):
            raise WritePlanError("root_plan_key is absent only before the first plan.")
        if (self.latest_internal_execution_id is None) != (self.last_plan_key is None):
            raise WritePlanError("latest_internal_execution_id exists exactly when a plan does.")
        if (self.active_plan_key is None) != (self.active_intent_id is None):
            raise WritePlanError("the owner active pair is both present or both absent.")
        if self.active_plan_key is not None and self.active_plan_key != self.last_plan_key:
            raise WritePlanError("the active plan is exactly the last persisted plan.")
        trio = (self.reserved_intent_id, self.reserved_intent_digest, self.reserved_generation)
        present = [member is not None for member in trio]
        if any(present) and not all(present):
            raise WritePlanError("the owner reservation trio is all present or all absent.")
        if self.reserved_intent_id is not None:
            if self.reserved_intent_digest != self.reserved_intent_id:
                raise WritePlanError("reserved_intent_digest is byte-equal to reserved_intent_id.")
            generation = self.reserved_generation
            if not _is_plain_int(generation) or generation != self.last_generation + 1:
                raise WritePlanError("reserved_generation is exactly last_generation + 1.")


def bootstrap_owner() -> SuccessorOwner:
    """Build the absent-owner bootstrap form: revision 0, no plan, generation -1."""
    return SuccessorOwner(
        revision=0,
        root_plan_key=None,
        last_plan_key=None,
        last_generation=-1,
        active_plan_key=None,
        active_intent_id=None,
        reserved_intent_id=None,
        reserved_intent_digest=None,
        reserved_generation=None,
        latest_internal_execution_id=None,
    )


# --- Closed intent META form ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IntentMeta:
    """The closed intent ``META`` form: authority inputs, state, and schedule/audit."""

    root_plan_key: str | None
    source_plan_key: str | None
    plan_digest: str
    intent_id: str
    roster_digest: str
    origin: IntentOrigin
    state: IntentState
    claimed_generation: int | None
    retry_count: int
    due_at: str
    ready_status: str | None
    ready_order: str | None
    created_at: str
    last_transition_at: str

    def __post_init__(self) -> None:
        _require_digest(self.plan_digest, "plan digest")
        _require_digest(self.intent_id, "intent id")
        _require_digest(self.roster_digest, "roster digest")
        _require_digest(self.origin.member, "origin member")
        if not _is_plain_int(self.retry_count) or self.retry_count < 0:
            raise WritePlanError("retry_count must be a nonnegative integer.")
        _require_instant(self.due_at, "due_at")
        _require_instant(self.created_at, "created_at")
        _require_instant(self.last_transition_at, "last_transition_at")
        pending = self.state is IntentState.PENDING
        if pending:
            if self.claimed_generation is not None:
                raise WritePlanError("a pending intent carries no claimed_generation.")
            if self.ready_status != READY_STATUS_VALUE:
                raise WritePlanError('a pending intent carries ready_status="ready".')
            if self.ready_order != ready_order_key(self.due_at, self.intent_id):
                raise WritePlanError("a pending intent's ready_order embeds its exact due.")
        else:
            generation = self.claimed_generation
            if generation is None or not _is_plain_int(generation) or generation < 0:
                raise WritePlanError("a claimed or admitted intent stores claimed_generation.")
            if self.ready_status is not None or self.ready_order is not None:
                raise WritePlanError("only a pending intent carries the ready field pair.")


# --- Closed plan META and plan UNIT# forms --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RequiredChildExpectation:
    """One sorted required-child expectation member: unit flow id to plan generation."""

    child_flow_id: str
    owner_generation: int

    def __post_init__(self) -> None:
        _require_identity(self.child_flow_id, "child flow id")
        if not _is_plain_int(self.owner_generation) or self.owner_generation < 0:
            raise WritePlanError("owner generation must be a nonnegative integer.")


@dataclass(frozen=True, slots=True)
class PlanMeta:
    """The closed plan ``META`` form persisted by ``PersistPlanRequest``."""

    plan_digest: str
    internal_execution_id: str
    root_plan_key: str
    generation: int
    source_plan_key: str | None
    origin_intent_id: str
    roster_digest: str
    origin: IntentOrigin
    status: PlanStatus
    unit_count: int
    admitted_count: int
    attested_count: int
    failed_count: int
    child_outcome_reduction_version: int
    required_child_expectations: tuple[RequiredChildExpectation, ...]
    settled_attempt_count: int
    settled_attempt_digest: str | None
    terminal_at: str | None
    created_at: str
    plan_reservation_count: int = 0
    """The permanent Begin reservation counter (``1..32``); absent on the item means zero.

    Begin maintains it on ``META`` outside every other builder's condition, so every
    post-state that rebuilds ``META`` from this form must carry the observed value or
    exact-replay classification misfires after the first Begin.
    """

    def __post_init__(self) -> None:
        _require_digest(self.plan_digest, "plan digest")
        if not _is_plain_int(self.plan_reservation_count) or not (
            0 <= self.plan_reservation_count <= MAX_PLAN_ATTEMPTS
        ):
            raise WritePlanError("plan_reservation_count must be within 0..32.")
        _require_digest(self.internal_execution_id, "internal execution id")
        _require_digest(self.origin_intent_id, "origin intent id")
        _require_digest(self.roster_digest, "roster digest")
        _require_digest(self.origin.member, "origin member")
        if not _is_plain_int(self.generation) or self.generation < 0:
            raise WritePlanError("plan generation must be a nonnegative integer.")
        if not 0 <= self.unit_count <= MAX_INTENT_UNITS:
            raise WritePlanError("unit_count must be within 0..15.")
        for name in ("admitted_count", "attested_count", "failed_count"):
            value: int = getattr(self, name)
            if not _is_plain_int(value) or not 0 <= value <= self.unit_count:
                raise WritePlanError(f"{name} must be within 0..unit_count.")
        if self.child_outcome_reduction_version != CHILD_OUTCOME_REDUCTION_VERSION:
            raise WritePlanError("child_outcome_reduction_version is exactly 2.")
        self._require_expectation_authority()
        if not 0 <= self.settled_attempt_count <= MAX_PLAN_ATTEMPTS:
            raise WritePlanError("settled_attempt_count must be within 0..32.")
        if self.settled_attempt_digest is not None:
            _require_digest(self.settled_attempt_digest, "settled attempt digest")
        if self.settled_attempt_count > 0 and self.settled_attempt_digest is None:
            raise WritePlanError("a nonzero settled-attempt count carries its digest.")
        terminal = self.status is not PlanStatus.PLANNED
        if terminal != (self.terminal_at is not None):
            raise WritePlanError("terminal_at exists exactly when the plan is terminal.")
        if self.terminal_at is not None:
            _require_instant(self.terminal_at, "terminal_at")
        _require_instant(self.created_at, "created_at")

    def _require_expectation_authority(self) -> None:
        members = self.required_child_expectations
        if len(members) != self.unit_count:
            raise WritePlanError("required-child expectations cover exactly every unit.")
        flows = [member.child_flow_id.encode("utf-8") for member in members]
        if flows != sorted(flows) or len(set(flows)) != len(flows):
            raise WritePlanError("required-child expectations are unique and byte-sorted.")
        if any(member.owner_generation != self.generation for member in members):
            raise WritePlanError("every required-child expectation binds the plan generation.")


@dataclass(frozen=True, slots=True)
class PlanUnit:
    """The closed plan ``UNIT#`` form as this slice creates and admits it."""

    unit: CanonicalUnitEntry
    flow_id: str
    owner_generation: int
    status: PlanUnitStatus
    admission: PlanUnitAdmission
    admission_deduplication_digest: str | None
    admitted_at: str | None
    created_at: str

    def __post_init__(self) -> None:
        _require_identity(self.flow_id, "unit flow id")
        if not _is_plain_int(self.owner_generation) or self.owner_generation < 0:
            raise WritePlanError("owner generation must be a nonnegative integer.")
        admitted = self.admission is PlanUnitAdmission.ADMITTED
        if admitted != (self.admission_deduplication_digest is not None) or admitted != (
            self.admitted_at is not None
        ):
            raise WritePlanError("admission audit exists exactly when the unit is admitted.")
        if self.admission_deduplication_digest is not None:
            _require_digest(self.admission_deduplication_digest, "deduplication digest")
        if self.admitted_at is not None:
            _require_instant(self.admitted_at, "admitted_at")
        _require_instant(self.created_at, "created_at")
