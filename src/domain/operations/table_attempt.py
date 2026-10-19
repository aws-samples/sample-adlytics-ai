# SPDX-License-Identifier: MIT-0
"""Pure ADR-075 §3.1 table-attempt authority: closed forms, predicates, and identities.

Everything here is deterministic: the closed ``ATTEMPT``/``FENCE``/``RUN#``/protected-case
item forms, the six-clause ``core_valid`` predicate and its closed complement, the
pending/blocked ``reconciliation_valid`` forms, ``repairable_reconciliation_metadata``,
and the pinned binding-digest / run-id / reconcile-key constructors over the §2.5
``canonical_json`` encoding. The attempt id, client request token, and envelope digest
follow the one pinned §5 step-2 constructor (``attempt_identity`` /
``recompute_attempt_identity``); ``core_valid`` still takes the recomputation as an
injected pure function checked by byte-equality against the stored values, keeping the
predicate independently testable. The adapter owns DynamoDB; this module owns every
byte-level decision.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from domain.operations.projection_handoff import canonical_json
from domain.operations.status import canonical_authority_digest

TABLE_ATTEMPT_PREFIX: Final = "TABLE_ATTEMPT#"
FENCE_RECORD_KEY: Final = "FENCE"
ATTEMPT_RECORD_PREFIX: Final = "ATTEMPT#"
RUN_RECORD_PREFIX: Final = "RUN#"
USAGE_RECORD_PREFIX: Final = "USAGE#"
PROTECTED_CASE_PREFIX: Final = "PROVIDER_RECONCILE_BLOCK#"
PROTECTED_CASE_RECORD_KEY: Final = "CASE"
UNIT_RECORD_PREFIX: Final = "UNIT#"

_INSTANT_TEXT: Final = re.compile(r"\A[0-9]{20}\Z")
_DIGEST_TEXT: Final = re.compile(r"\A[0-9a-f]{64}\Z")
_UUID4_TEXT: Final = re.compile(
    r"\A[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
_PENDING_KEY_TEXT: Final = re.compile(r"\A[0-9]{20}#[0-9a-f]{64}\Z")
_MICROS_DIGITS: Final = 20


class TableAttemptError(ValueError):
    """A table-attempt item, key, or identity violates the closed §3.1 contract."""


class ProviderErrorClass(StrEnum):
    """Closed sanitized-evidence error classification; ``Accepted`` is always ``none``."""

    THROTTLING = "throttling"
    SERVICE_UNAVAILABLE = "service_unavailable"
    NETWORK_TIMEOUT = "network_timeout"
    RESPONSE_MALFORMED = "response_malformed"
    UNKNOWN = "unknown"
    NONE = "none"


class ProviderTerminalState(StrEnum):
    """The only three provider-terminal attempt states."""

    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class WriterKind(StrEnum):
    """Closed inline-run writer vocabulary."""

    TRANSITIONAL_DELIVERY = "transitional_delivery"
    INGEST_DELIVERY_OPERATOR = "ingest_delivery_operator"
    REPROCESS_SEMANTIC_V2_OPERATOR = "reprocess_semantic_v2_operator"
    GRAPH_UNIT = "graph_unit"
    """S6 §2.5 (D5): TableWriter executing one claimed `write_unit:<k>` graph node inline."""


class AttemptOwnerKind(StrEnum):
    """Closed §5 step-2 owner vocabulary for the attempt-identity tuple."""

    QUEUED_UNIT = "queued_unit"
    INLINE_RUN = "inline_run"


class InlineRunState(StrEnum):
    """Closed mutable ``RUN#`` state; ``reconciled`` is nonterminal and resumable."""

    OPEN = "open"
    CANCEL_REQUESTED = "cancel_requested"
    RECONCILED = "reconciled"
    TERMINAL = "terminal"


class ReconcileBlockReason(StrEnum):
    """The only two typed reconciliation-block reasons."""

    MALFORMED_RECONCILIATION_METADATA = "MALFORMED_RECONCILIATION_METADATA"
    SAME_SNAPSHOT_RESCHEDULE_REJECTED = "SAME_SNAPSHOT_RESCHEDULE_REJECTED"


class ProtectedCaseState(StrEnum):
    """A protected case is ``blocked`` until requeue leaves it as ``requeued`` audit."""

    BLOCKED = "blocked"
    REQUEUED = "requeued"


# --- Shared validators ----------------------------------------------------------------------------


def is_canonical_table_id(value: str) -> bool:
    """Whether text is a canonical database-qualified physical table id."""
    database, separator, table = value.partition(".")
    return bool(database.strip() and separator and table.strip() and value == value.strip())


def is_canonical_instant(value: str) -> bool:
    """Whether text is a canonical 20-digit UTC epoch-microseconds instant."""
    return bool(_INSTANT_TEXT.match(value))


def valid_query_execution_id(value: object) -> bool:
    """Whether a value is a String with at least one non-whitespace character (§5)."""
    return isinstance(value, str) and bool(value.strip())


def _require_canonical_table(value: str, name: str) -> None:
    if not is_canonical_table_id(value):
        raise TableAttemptError(f"{name} must be a canonical database-qualified table id.")


def _require_nonblank(value: str, name: str) -> None:
    if not value.strip():
        raise TableAttemptError(f"{name} must be nonblank text.")


def _require_instant(value: str, name: str) -> None:
    if not is_canonical_instant(value):
        raise TableAttemptError(f"{name} must be a 20-digit epoch-microseconds instant.")


def _is_plain_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# --- Canonical keys and pinned identity constructors ----------------------------------------------


def table_attempt_partition_key(table_id: str) -> str:
    """Return ``TABLE_ATTEMPT#<physical table id>`` for a canonical table id."""
    _require_canonical_table(table_id, "physical table id")
    return f"{TABLE_ATTEMPT_PREFIX}{table_id}"


def attempt_record_key(attempt_id: str) -> str:
    """Return ``ATTEMPT#<attempt id>``."""
    _require_nonblank(attempt_id, "attempt id")
    return f"{ATTEMPT_RECORD_PREFIX}{attempt_id}"


def run_record_key(run_id: str) -> str:
    """Return ``RUN#<run id>``."""
    if not _DIGEST_TEXT.match(run_id):
        raise TableAttemptError("run id must be 64 lowercase hex characters.")
    return f"{RUN_RECORD_PREFIX}{run_id}"


def protected_case_partition_key(recovery_ref: str) -> str:
    """Return ``PROVIDER_RECONCILE_BLOCK#<reconcile_recovery_ref>``."""
    if not _UUID4_TEXT.match(recovery_ref):
        raise TableAttemptError("recovery reference must be a lowercase UUIDv4.")
    return f"{PROTECTED_CASE_PREFIX}{recovery_ref}"


@dataclass(frozen=True, slots=True)
class BindingDigestRequest:
    """The exact ordered §3.1 binding-digest inputs."""

    physical_table_id: str
    attempt_id: str
    lease_holder: str
    fencing_token: int

    def __post_init__(self) -> None:
        _require_canonical_table(self.physical_table_id, "physical table id")
        _require_nonblank(self.attempt_id, "attempt id")
        _require_nonblank(self.lease_holder, "lease holder")
        if self.fencing_token < 1:
            raise TableAttemptError("fencing token must be positive.")


def binding_digest(request: BindingDigestRequest) -> str:
    """Derive ``sha256(canonical_json([table, attempt, holder, token]))`` as lowercase hex."""
    payload = canonical_json(
        [
            request.physical_table_id,
            request.attempt_id,
            request.lease_holder,
            request.fencing_token,
        ]
    )
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class RunIdentityRequest:
    """The exact ordered §3.1 ``run_id`` tuple."""

    writer_kind: WriterKind
    operation_id: str
    execution_id: str
    physical_table_id: str

    def __post_init__(self) -> None:
        _require_nonblank(self.operation_id, "operation id")
        _require_nonblank(self.execution_id, "execution id")
        _require_canonical_table(self.physical_table_id, "physical table id")


def run_identity(request: RunIdentityRequest) -> str:
    """Derive ``sha256(UTF8(canonical_json([kind, operation, execution, table])))`` as hex."""
    payload = canonical_json(
        [
            request.writer_kind.value,
            request.operation_id,
            request.execution_id,
            request.physical_table_id,
        ]
    )
    return hashlib.sha256(payload).hexdigest()


def pending_reconcile_key(due_epoch_micros: int, digest: str) -> str:
    """Render the pending GSI sort key ``<20-digit epoch-micros>#<binding digest>``."""
    if not _is_plain_int(due_epoch_micros) or due_epoch_micros < 0:
        raise TableAttemptError("due instant must be a nonnegative integer of epoch microseconds.")
    rendered = f"{due_epoch_micros:020d}"
    if len(rendered) != _MICROS_DIGITS:
        raise TableAttemptError("due instant overflows the 20-digit canonical form.")
    if not _DIGEST_TEXT.match(digest):
        raise TableAttemptError("binding digest must be 64 lowercase hex characters.")
    return f"{rendered}#{digest}"


def blocked_reconcile_key(recovery_ref: str) -> str:
    """Render the blocked GSI sort key ``BLOCK#<opaque reconcile_recovery_ref>``."""
    if not _UUID4_TEXT.match(recovery_ref):
        raise TableAttemptError("recovery reference must be a lowercase UUIDv4.")
    return f"BLOCK#{recovery_ref}"


RECONCILE_OBSERVATION_NAMESPACE: Final = "stage1-reconcile-observation/v1"
RECONCILE_BLOCK_NAMESPACE: Final = "stage1-reconcile-block/v1"
RECONCILE_REQUEUE_NAMESPACE: Final = "stage1-reconcile-requeue/v1"

_RECONCILE_BACKOFF_SECONDS: Final = (60, 120, 240, 480, 900)


def reconcile_backoff_delay_seconds(old_count: int) -> int:
    """Return the exact §5 delay for old count ``c``: 60, 120, 240, 480, then 900."""
    if not _is_plain_int(old_count) or old_count < 0:
        raise TableAttemptError("reconcile attempt count must be a nonnegative integer.")
    return _RECONCILE_BACKOFF_SECONDS[min(old_count, len(_RECONCILE_BACKOFF_SECONDS) - 1)]


def rescheduled_reconcile_due_micros(operation_instant: str, old_count: int) -> int:
    """Compute the §5 due instant: operation instant plus the exact backoff, integers only."""
    _require_instant(operation_instant, "operation instant")
    return int(operation_instant) + reconcile_backoff_delay_seconds(old_count) * 1_000_000


# --- Sanitized start evidence ---------------------------------------------------------------------

_EVIDENCE_FIELD_NAMES: Final = frozenset({"provider_status_code", "provider_error_class"})


@dataclass(frozen=True, slots=True)
class SanitizedStartEvidence:
    """The closed two-field §5 evidence form; no free text ever enters it."""

    provider_status_code: int | None
    provider_error_class: ProviderErrorClass

    def __post_init__(self) -> None:
        code = self.provider_status_code
        if code is not None and not _is_plain_int(code):
            raise TableAttemptError("provider status code must be an integer or null.")


def parse_sanitized_evidence(fields: Mapping[str, object]) -> SanitizedStartEvidence:
    """Accept only the exact closed two-field evidence map."""
    if frozenset(fields) != _EVIDENCE_FIELD_NAMES:
        raise TableAttemptError("sanitized evidence differs from the closed two-field form.")
    code = fields["provider_status_code"]
    status: int | None = None
    if code is not None:
        if isinstance(code, bool) or not isinstance(code, int):
            raise TableAttemptError("provider status code must be an integer or null.")
        status = code
    error_class = fields["provider_error_class"]
    if not isinstance(error_class, str) or error_class not in {
        member.value for member in ProviderErrorClass
    }:
        raise TableAttemptError("provider error class is outside the closed vocabulary.")
    return SanitizedStartEvidence(
        provider_status_code=status,
        provider_error_class=ProviderErrorClass(error_class),
    )


# --- Closed ATTEMPT forms -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AttemptCore:
    """The immutable attempt core with exactly one owner binding (§3.1 clause 4).

    ``attempt_id``, ``client_request_token``, and ``envelope_digest`` are stored
    values derived by the one pinned §5 step-2 constructor; ``core_valid`` checks
    them by byte-equality against an injected pure recomputation, with
    ``recompute_attempt_identity`` as the production hook.
    """

    physical_table_id: str
    attempt_id: str
    plan_unit_owner_ref: str | None
    inline_run_owner_ref: str | None
    ordinal: int
    statement_kind: str
    lease_holder: str
    fencing_token: int
    writer_execution_id: str
    start_envelope: str
    client_request_token: str
    envelope_digest: str
    reservation_identity: str

    def __post_init__(self) -> None:
        _require_canonical_table(self.physical_table_id, "physical table id")
        _require_nonblank(self.attempt_id, "attempt id")
        if (self.plan_unit_owner_ref is None) == (self.inline_run_owner_ref is None):
            raise TableAttemptError("an attempt carries exactly one owner binding.")
        owner_ref = self.plan_unit_owner_ref or self.inline_run_owner_ref or ""
        _require_nonblank(owner_ref, "owner binding reference")
        if self.ordinal < 1:
            raise TableAttemptError("ordinal must be a positive integer.")
        _require_nonblank(self.statement_kind, "statement kind")
        _require_nonblank(self.lease_holder, "lease holder")
        if self.fencing_token < 1:
            raise TableAttemptError("fencing token must be positive.")
        _require_nonblank(self.writer_execution_id, "writer execution id")
        _require_nonblank(self.start_envelope, "start envelope")
        _require_nonblank(self.client_request_token, "client request token")
        _require_nonblank(self.envelope_digest, "envelope digest")
        _require_nonblank(self.reservation_identity, "reservation identity")


@dataclass(frozen=True, slots=True)
class IntentAttempt:
    """``intent``: immutable core only; every later field family is forbidden."""

    core: AttemptCore


@dataclass(frozen=True, slots=True)
class SubmittedAttempt:
    """``submitted``: core plus the write-once valid-query/submission/evidence triple."""

    core: AttemptCore
    query_execution_id: str
    submitted_at: str
    sanitized_start_evidence: SanitizedStartEvidence

    def __post_init__(self) -> None:
        if not valid_query_execution_id(self.query_execution_id):
            raise TableAttemptError("query execution id must contain non-whitespace text.")
        _require_instant(self.submitted_at, "submitted_at")


@dataclass(frozen=True, slots=True)
class CancelRequestedAttempt:
    """``cancel_requested``: the submitted triple plus one cancellation instant."""

    core: AttemptCore
    query_execution_id: str
    submitted_at: str
    sanitized_start_evidence: SanitizedStartEvidence
    cancel_requested_at: str

    def __post_init__(self) -> None:
        if not valid_query_execution_id(self.query_execution_id):
            raise TableAttemptError("query execution id must contain non-whitespace text.")
        _require_instant(self.submitted_at, "submitted_at")
        _require_instant(self.cancel_requested_at, "cancel_requested_at")


@dataclass(frozen=True, slots=True)
class ParkedAttempt:
    """``parked``: core plus park instant; the query triple is all-present or all-absent."""

    core: AttemptCore
    parked_at: str
    query_execution_id: str | None = None
    submitted_at: str | None = None
    sanitized_start_evidence: SanitizedStartEvidence | None = None
    cancel_requested_at: str | None = None

    def __post_init__(self) -> None:
        _require_instant(self.parked_at, "parked_at")
        triple = (self.query_execution_id, self.submitted_at, self.sanitized_start_evidence)
        present = [member is not None for member in triple]
        if any(present) and not all(present):
            raise TableAttemptError(
                "a parked attempt permits only the absent pair or the complete query triple."
            )
        if self.query_execution_id is not None:
            if not valid_query_execution_id(self.query_execution_id):
                raise TableAttemptError("query execution id must contain non-whitespace text.")
            _require_instant(self.submitted_at or "", "submitted_at")
        if self.cancel_requested_at is not None:
            if not all(present):
                raise TableAttemptError(
                    "a parked cancellation instant requires the valid-query triple."
                )
            _require_instant(self.cancel_requested_at, "cancel_requested_at")


@dataclass(frozen=True, slots=True)
class TerminalAttempt:
    """One provider-terminal attempt: the sole authority for its Athena quantities.

    Each quantity is exactly known (a nonnegative integer) or missing (``None``).
    ``terminal_at`` and ``athena_usage_state`` are written by
    ``CompleteTableAttemptRequest``; they stay optional here so pre-completion
    observations of the closed form remain constructible unchanged.
    """

    core: AttemptCore
    outcome: ProviderTerminalState
    query_execution_id: str
    submitted_at: str
    sanitized_start_evidence: SanitizedStartEvidence
    cancel_requested_at: str | None = None
    parked_at: str | None = None
    query_count: int | None = None
    scanned_bytes: int | None = None
    engine_ms: int | None = None
    terminal_at: str | None = None
    athena_usage_state: str | None = None
    # A settled attempt also stores the immutable owner disposition tag, the concept-derived usage
    # category, and the settlement authority digest (§3.1); the three exist exactly when settled.
    owner_disposition: str | None = None
    usage_category: str | None = None
    usage_authority_digest: str | None = None

    def __post_init__(self) -> None:
        if not valid_query_execution_id(self.query_execution_id):
            raise TableAttemptError("query execution id must contain non-whitespace text.")
        settled = self.athena_usage_state == "settled"
        settlement = (self.owner_disposition, self.usage_category, self.usage_authority_digest)
        if any(settled != (member is not None) for member in settlement):
            raise TableAttemptError("settlement attributes exist exactly when usage is settled.")
        if self.owner_disposition is not None:
            _require_nonblank(self.owner_disposition, "owner_disposition")
        if self.usage_category is not None:
            _require_nonblank(self.usage_category, "usage_category")
        if self.usage_authority_digest is not None and not _DIGEST_TEXT.match(
            self.usage_authority_digest
        ):
            raise TableAttemptError("usage authority digest must be 64 lowercase hex characters.")
        _require_instant(self.submitted_at, "submitted_at")
        if self.cancel_requested_at is not None:
            _require_instant(self.cancel_requested_at, "cancel_requested_at")
        if self.parked_at is not None:
            _require_instant(self.parked_at, "parked_at")
        for name in ("query_count", "scanned_bytes", "engine_ms"):
            value: int | None = getattr(self, name)
            if value is not None and (not _is_plain_int(value) or value < 0):
                raise TableAttemptError(f"{name} must be a nonnegative integer when known.")
        if self.terminal_at is not None:
            _require_instant(self.terminal_at, "terminal_at")
        if self.athena_usage_state is not None and self.athena_usage_state not in (
            "unsettled",
            "settled",
        ):
            raise TableAttemptError("athena usage state must be unsettled or settled.")


type NonterminalTableAttempt = (
    IntentAttempt | SubmittedAttempt | CancelRequestedAttempt | ParkedAttempt
)
type TableAttempt = NonterminalTableAttempt | TerminalAttempt


# --- Closed FENCE forms ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ActiveFence:
    """``active``: table/attempt/holder/token, no park instant, no other field."""

    physical_table_id: str
    attempt_id: str
    lease_holder: str
    fencing_token: int

    def __post_init__(self) -> None:
        _require_canonical_table(self.physical_table_id, "physical table id")
        _require_nonblank(self.attempt_id, "attempt id")
        _require_nonblank(self.lease_holder, "lease holder")
        if self.fencing_token < 1:
            raise TableAttemptError("fencing token must be positive.")


@dataclass(frozen=True, slots=True)
class ParkedFence:
    """``parked``: the active fields plus one canonical park instant."""

    physical_table_id: str
    attempt_id: str
    lease_holder: str
    fencing_token: int
    parked_at: str

    def __post_init__(self) -> None:
        _require_canonical_table(self.physical_table_id, "physical table id")
        _require_nonblank(self.attempt_id, "attempt id")
        _require_nonblank(self.lease_holder, "lease holder")
        if self.fencing_token < 1:
            raise TableAttemptError("fencing token must be positive.")
        _require_instant(self.parked_at, "parked_at")


@dataclass(frozen=True, slots=True)
class ClearFence:
    """``clear``: no attempt binding and no reconciliation attribute may remain."""

    physical_table_id: str

    def __post_init__(self) -> None:
        _require_canonical_table(self.physical_table_id, "physical table id")


type TableAttemptFence = ActiveFence | ParkedFence | ClearFence


# --- Closed RUN# form -----------------------------------------------------------------------------

_RUN_RESERVATION_CAP: Final = 10


@dataclass(frozen=True, slots=True)
class InlineRun:
    """One durable per-run, per-table inline accounting item (§3.1 ``RUN#``).

    ``run_id`` must reproduce from the immutable tuple; ``__post_init__`` enforces
    the pinned constructor. The item stores attempt references, never provider
    quantity values.
    """

    run_id: str
    writer_kind: WriterKind
    operation_id: str
    execution_id: str
    physical_table_id: str
    status_flow_binding: str
    execution_binding: str
    table_kind: str
    statement_intent_kinds: tuple[str, ...]
    pointer_finalization_payload: str | None
    created_at: str
    state: InlineRunState
    current_attempt_id: str | None
    next_ordinal: int
    accepted_attempt_reservations: int
    terminal_outcome: str | None
    terminal_at: str | None
    attempt_references: tuple[str, ...]
    status_outbox_disposition: str | None
    usage_outbox_disposition: str | None

    def __post_init__(self) -> None:
        expected = run_identity(
            RunIdentityRequest(
                writer_kind=self.writer_kind,
                operation_id=self.operation_id,
                execution_id=self.execution_id,
                physical_table_id=self.physical_table_id,
            )
        )
        if self.run_id != expected:
            raise TableAttemptError("run id does not reproduce from its immutable tuple.")
        _require_nonblank(self.status_flow_binding, "status flow binding")
        _require_nonblank(self.execution_binding, "execution binding")
        _require_nonblank(self.table_kind, "table kind")
        if not self.statement_intent_kinds or any(
            not kind.strip() for kind in self.statement_intent_kinds
        ):
            raise TableAttemptError("statement intent kinds must be a nonempty ordered roster.")
        _require_instant(self.created_at, "created_at")
        if self.next_ordinal < 1:
            raise TableAttemptError("next ordinal must be a positive integer.")
        if not 0 <= self.accepted_attempt_reservations <= _RUN_RESERVATION_CAP:
            raise TableAttemptError("accepted attempt reservations must be within 0..10.")
        terminal = self.state is InlineRunState.TERMINAL
        if terminal != (self.terminal_outcome is not None) or terminal != (
            self.terminal_at is not None
        ):
            raise TableAttemptError("terminal outcome and instant exist exactly when terminal.")
        if self.terminal_at is not None:
            _require_instant(self.terminal_at, "terminal_at")
        if self.current_attempt_id is not None:
            _require_nonblank(self.current_attempt_id, "current attempt id")


# --- Closed protected-case form -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProtectedReconcileCase:
    """The retained ``PROVIDER_RECONCILE_BLOCK#<ref>/CASE`` discovery-isolation item."""

    recovery_ref: str
    state: ProtectedCaseState
    physical_table_id: str
    attempt_id: str
    reconcile_block_id: str
    binding_digest: str
    reason: ReconcileBlockReason
    blocked_at: str
    observation_digest: str | None
    requeued_at: str | None
    requeue_id: str | None = None
    requeue_operation_id: str | None = None
    resolution_reason: str | None = None

    def __post_init__(self) -> None:
        if not _UUID4_TEXT.match(self.recovery_ref):
            raise TableAttemptError("recovery reference must be a lowercase UUIDv4.")
        _require_canonical_table(self.physical_table_id, "physical table id")
        _require_nonblank(self.attempt_id, "attempt id")
        _require_nonblank(self.reconcile_block_id, "reconcile block id")
        if not _DIGEST_TEXT.match(self.binding_digest):
            raise TableAttemptError("binding digest must be 64 lowercase hex characters.")
        _require_instant(self.blocked_at, "blocked_at")
        if self.observation_digest is not None and not _DIGEST_TEXT.match(self.observation_digest):
            raise TableAttemptError("observation digest must be 64 lowercase hex characters.")
        requeued = self.state is ProtectedCaseState.REQUEUED
        audit = (
            self.requeued_at,
            self.requeue_id,
            self.requeue_operation_id,
            self.resolution_reason,
        )
        if any(requeued != (member is not None) for member in audit):
            raise TableAttemptError("requeue audit exists exactly when the case is requeued.")
        if self.requeued_at is not None:
            _require_instant(self.requeued_at, "requeued_at")
        if self.requeue_id is not None and not _DIGEST_TEXT.match(self.requeue_id):
            raise TableAttemptError("requeue id must be 64 lowercase hex characters.")
        if self.requeue_operation_id is not None:
            _require_nonblank(self.requeue_operation_id, "requeue operation id")
        if self.resolution_reason is not None:
            _require_nonblank(self.resolution_reason, "resolution reason")


# --- Typed observations (the store reads; the predicates decide) ----------------------------------

type RawValue = str | int | bool

RECONCILIATION_FIELD_ORDER: Final = (
    "reconcile_status",
    "reconcile_after_or_block",
    "reconcile_attempt_count",
    "requeued_from_case_ref",
    "requeued_at",
    "reconcile_block_id",
    "reconcile_block_reason",
    "reconcile_blocked_at",
    "reconcile_observation_digest",
    "reconcile_recovery_ref",
)
RECONCILIATION_FIELD_NAMES: Final = frozenset(RECONCILIATION_FIELD_ORDER)


@dataclass(frozen=True, slots=True)
class ReconciliationObservation:
    """The exact presence/type/value snapshot of every reconciliation field."""

    reconcile_status: RawValue | None = None
    reconcile_after_or_block: RawValue | None = None
    reconcile_attempt_count: RawValue | None = None
    requeued_from_case_ref: RawValue | None = None
    requeued_at: RawValue | None = None
    reconcile_block_id: RawValue | None = None
    reconcile_block_reason: RawValue | None = None
    reconcile_blocked_at: RawValue | None = None
    reconcile_observation_digest: RawValue | None = None
    reconcile_recovery_ref: RawValue | None = None
    malformed_field_names: frozenset[str] = frozenset()

    @property
    def is_empty(self) -> bool:
        """Whether no reconciliation attribute was present at all."""
        values = (
            self.reconcile_status,
            self.reconcile_after_or_block,
            self.reconcile_attempt_count,
            self.requeued_from_case_ref,
            self.requeued_at,
            self.reconcile_block_id,
            self.reconcile_block_reason,
            self.reconcile_blocked_at,
            self.reconcile_observation_digest,
            self.reconcile_recovery_ref,
        )
        return all(value is None for value in values) and not self.malformed_field_names


def reconcile_observation_digest(variant: str, observation: ReconciliationObservation) -> str:
    """Derive the pinned digest of one exact reconciliation presence/type/value snapshot.

    Every reconciliation field contributes one ordered ``[name, tag, value]``
    member: ``absent`` and ``malformed`` carry ``None``, while ``text``,
    ``integer``, and ``boolean`` carry the exact retained value. The typed block
    variant name participates so the two §3.2 observation forms never collide.
    """
    _require_nonblank(variant, "observation variant")
    members: list[object] = []
    for name in RECONCILIATION_FIELD_ORDER:
        value: RawValue | None = getattr(observation, name)
        if name in observation.malformed_field_names:
            members.append([name, "malformed", None])
        elif value is None:
            members.append([name, "absent", None])
        elif isinstance(value, bool):
            members.append([name, "boolean", value])
        elif isinstance(value, int):
            members.append([name, "integer", value])
        else:
            members.append([name, "text", value])
    return canonical_authority_digest([RECONCILE_OBSERVATION_NAMESPACE, variant, members])


@dataclass(frozen=True, slots=True)
class ReconcileBlockIdentityRequest:
    """The exact ordered inputs of the pinned deterministic block identity."""

    binding_digest: str
    reason: ReconcileBlockReason
    observation_digest: str
    blocked_at: str

    def __post_init__(self) -> None:
        if not _DIGEST_TEXT.match(self.binding_digest):
            raise TableAttemptError("binding digest must be 64 lowercase hex characters.")
        if not _DIGEST_TEXT.match(self.observation_digest):
            raise TableAttemptError("observation digest must be 64 lowercase hex characters.")
        _require_instant(self.blocked_at, "blocked_at")


def reconcile_block_identity(request: ReconcileBlockIdentityRequest) -> str:
    """Derive the sole deterministic ``reconcile_block_id`` for one typed block."""
    return canonical_authority_digest(
        [
            RECONCILE_BLOCK_NAMESPACE,
            request.binding_digest,
            request.reason.value,
            request.observation_digest,
            request.blocked_at,
        ]
    )


def reconcile_recovery_reference(block_id: str) -> str:
    """Derive the opaque canonical lowercase UUIDv4-form recovery reference.

    The reference is a hash of the deterministic block id rendered in the closed
    UUIDv4 form, so it embeds no selector, exact replay reproduces identical
    bytes, and a later reblock (a different block identity) allocates a fresh
    reference. No other derivation exists.
    """
    if not _DIGEST_TEXT.match(block_id):
        raise TableAttemptError("block id must be 64 lowercase hex characters.")
    seed = f"{RECONCILE_BLOCK_NAMESPACE}#recovery-ref#{block_id}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    body = list(digest[:32])
    body[12] = "4"
    body[16] = "89ab"[int(digest[32], 16) % 4]
    text = "".join(body)
    return f"{text[:8]}-{text[8:12]}-{text[12:16]}-{text[16:20]}-{text[20:32]}"


def reconcile_requeue_identity(recovery_ref: str, operation_id: str) -> str:
    """Derive the deterministic requeue identity for one recovery-broker operation."""
    if not _UUID4_TEXT.match(recovery_ref):
        raise TableAttemptError("recovery reference must be a lowercase UUIDv4.")
    _require_nonblank(operation_id, "operation id")
    return canonical_authority_digest([RECONCILE_REQUEUE_NAMESPACE, recovery_ref, operation_id])


@dataclass(frozen=True, slots=True)
class FenceObservation:
    """One strongly read raw ``FENCE`` item; missing fields are ``None``."""

    partition_key: str
    record_key: str
    state: str | None
    physical_table_id: str | None
    attempt_id: str | None
    lease_holder: str | None
    fencing_token: int | None
    parked_at: str | None
    reconciliation: ReconciliationObservation = ReconciliationObservation()
    malformed_field_names: frozenset[str] = frozenset()
    unrecognized_field_names: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class AttemptObservation:
    """One strongly read raw ``ATTEMPT#`` item; missing fields are ``None``."""

    partition_key: str
    record_key: str
    state: str | None
    physical_table_id: str | None
    attempt_id: str | None
    plan_unit_owner_ref: str | None
    inline_run_owner_ref: str | None
    ordinal: int | None
    statement_kind: str | None
    lease_holder: str | None
    fencing_token: int | None
    writer_execution_id: str | None
    start_envelope: str | None
    client_request_token: str | None
    envelope_digest: str | None
    reservation_identity: str | None
    query_execution_id: str | None = None
    submitted_at: str | None = None
    sanitized_start_evidence: SanitizedStartEvidence | None = None
    cancel_requested_at: str | None = None
    parked_at: str | None = None
    query_count: int | None = None
    scanned_bytes: int | None = None
    engine_ms: int | None = None
    terminal_at: str | None = None
    athena_usage_state: str | None = None
    owner_disposition: str | None = None
    usage_category: str | None = None
    usage_authority_digest: str | None = None
    malformed_field_names: frozenset[str] = frozenset()
    unrecognized_field_names: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class PlanUnitOwnerObservation:
    """The strongly read queued-owner facts §3.1 clause 6 requires."""

    plan_partition_key: str
    unit_record_key: str
    plan_meta_exists: bool
    origin_intent_admitted: bool
    unit_status: str | None
    writer_execution_id: str | None
    ordinal: int | None
    current_attempt_id: str | None
    lease_holder: str | None
    fencing_token: int | None
    unit_reservation_count: int | None
    plan_reservation_count: int | None
    reserved_slot_identity: str | None


@dataclass(frozen=True, slots=True)
class InlineRunOwnerObservation:
    """The strongly read inline-run-owner facts §3.1 clause 6 requires."""

    run_partition_key: str
    run_record_key: str
    run_id: str
    state: str | None
    writer_execution_id: str | None
    ordinal: int | None
    current_attempt_id: str | None
    lease_holder: str | None
    fencing_token: int | None
    reservation_count: int | None
    reserved_slot_identity: str | None


# --- Parsing observations into the closed forms ---------------------------------------------------


def _fence_binding(observation: FenceObservation) -> tuple[str, str, str, int]:
    table = observation.physical_table_id
    attempt = observation.attempt_id
    holder = observation.lease_holder
    token = observation.fencing_token
    if table is None or attempt is None or holder is None or token is None:
        raise TableAttemptError("fence safety-core fields are incomplete.")
    return table, attempt, holder, token


def parse_fence(observation: FenceObservation) -> TableAttemptFence:
    """Accept only the closed ``active``/``parked``/``clear`` fence forms."""
    if observation.malformed_field_names or observation.unrecognized_field_names:
        raise TableAttemptError("fence carries malformed or extra safety-core fields.")
    table = observation.physical_table_id
    if table is None or observation.partition_key != table_attempt_partition_key(table):
        raise TableAttemptError("fence partition key is outside the canonical form.")
    if observation.record_key != FENCE_RECORD_KEY:
        raise TableAttemptError("fence sort key must be exactly FENCE.")
    if observation.state == "clear":
        empty = (
            observation.attempt_id is None
            and observation.lease_holder is None
            and observation.fencing_token is None
            and observation.parked_at is None
            and observation.reconciliation.is_empty
        )
        if not empty:
            raise TableAttemptError("a clear fence carries no binding or reconciliation fields.")
        return ClearFence(physical_table_id=table)
    if observation.state == "active":
        if observation.parked_at is not None:
            raise TableAttemptError("an active fence forbids a park instant.")
        table, attempt, holder, token = _fence_binding(observation)
        return ActiveFence(table, attempt, holder, token)
    if observation.state == "parked":
        if observation.parked_at is None:
            raise TableAttemptError("a parked fence requires a canonical park instant.")
        table, attempt, holder, token = _fence_binding(observation)
        return ParkedFence(table, attempt, holder, token, observation.parked_at)
    raise TableAttemptError("fence state is outside the closed vocabulary.")


def _require_text(value: str | None, name: str) -> str:
    if value is None:
        raise TableAttemptError(f"{name} is missing from the attempt core.")
    return value


def _attempt_core(observation: AttemptObservation) -> AttemptCore:
    ordinal = observation.ordinal
    token = observation.fencing_token
    if ordinal is None or token is None:
        raise TableAttemptError("attempt core integer fields are incomplete.")
    return AttemptCore(
        physical_table_id=_require_text(observation.physical_table_id, "physical table id"),
        attempt_id=_require_text(observation.attempt_id, "attempt id"),
        plan_unit_owner_ref=observation.plan_unit_owner_ref,
        inline_run_owner_ref=observation.inline_run_owner_ref,
        ordinal=ordinal,
        statement_kind=_require_text(observation.statement_kind, "statement kind"),
        lease_holder=_require_text(observation.lease_holder, "lease holder"),
        fencing_token=token,
        writer_execution_id=_require_text(observation.writer_execution_id, "writer execution id"),
        start_envelope=_require_text(observation.start_envelope, "start envelope"),
        client_request_token=_require_text(
            observation.client_request_token, "client request token"
        ),
        envelope_digest=_require_text(observation.envelope_digest, "envelope digest"),
        reservation_identity=_require_text(
            observation.reservation_identity, "reservation identity"
        ),
    )


_OPTIONAL_ATTEMPT_FIELDS: Final = (
    "query_execution_id",
    "submitted_at",
    "sanitized_start_evidence",
    "cancel_requested_at",
    "parked_at",
    "query_count",
    "scanned_bytes",
    "engine_ms",
    "terminal_at",
    "athena_usage_state",
    "owner_disposition",
    "usage_category",
    "usage_authority_digest",
)
_SUBMITTED_FIELDS: Final = frozenset(
    {"query_execution_id", "submitted_at", "sanitized_start_evidence"}
)
_CANCEL_FIELDS: Final = _SUBMITTED_FIELDS | {"cancel_requested_at"}
_PARKED_FIELDS: Final = _CANCEL_FIELDS | {"parked_at"}
_TERMINAL_FIELDS: Final = _PARKED_FIELDS | {
    "query_count",
    "scanned_bytes",
    "engine_ms",
    "terminal_at",
    "athena_usage_state",
    "owner_disposition",
    "usage_category",
    "usage_authority_digest",
}


def _forbid_outside(observation: AttemptObservation, allowed: frozenset[str]) -> None:
    for name in _OPTIONAL_ATTEMPT_FIELDS:
        if name not in allowed and getattr(observation, name) is not None:
            raise TableAttemptError(f"{name} is forbidden in this attempt state.")


def _submitted_triple(
    observation: AttemptObservation,
) -> tuple[str, str, SanitizedStartEvidence]:
    query = observation.query_execution_id
    submitted_at = observation.submitted_at
    evidence = observation.sanitized_start_evidence
    if query is None or submitted_at is None or evidence is None:
        raise TableAttemptError("the valid-query/submission/evidence triple is incomplete.")
    return query, submitted_at, evidence


def _parse_nonterminal(observation: AttemptObservation, core: AttemptCore) -> TableAttempt:
    state = observation.state
    if state == "intent":
        _forbid_outside(observation, frozenset())
        return IntentAttempt(core)
    if state == "submitted":
        _forbid_outside(observation, _SUBMITTED_FIELDS)
        return SubmittedAttempt(core, *_submitted_triple(observation))
    if state == "cancel_requested":
        _forbid_outside(observation, _CANCEL_FIELDS)
        cancel_at = _require_text(observation.cancel_requested_at, "cancel_requested_at")
        return CancelRequestedAttempt(core, *_submitted_triple(observation), cancel_at)
    _forbid_outside(observation, _PARKED_FIELDS)
    return ParkedAttempt(
        core,
        _require_text(observation.parked_at, "parked_at"),
        query_execution_id=observation.query_execution_id,
        submitted_at=observation.submitted_at,
        sanitized_start_evidence=observation.sanitized_start_evidence,
        cancel_requested_at=observation.cancel_requested_at,
    )


def parse_attempt(observation: AttemptObservation) -> TableAttempt:
    """Accept only the closed nonterminal and provider-terminal attempt forms."""
    if observation.malformed_field_names or observation.unrecognized_field_names:
        raise TableAttemptError("attempt carries malformed or extra core fields.")
    core = _attempt_core(observation)
    if observation.partition_key != table_attempt_partition_key(core.physical_table_id):
        raise TableAttemptError("attempt partition key is outside the canonical form.")
    if observation.record_key != attempt_record_key(core.attempt_id):
        raise TableAttemptError("attempt sort key must be exactly ATTEMPT#<attempt id>.")
    state = observation.state
    if state is not None and state in {member.value for member in ProviderTerminalState}:
        _forbid_outside(observation, _TERMINAL_FIELDS)
        return TerminalAttempt(
            core,
            ProviderTerminalState(state),
            *_submitted_triple(observation),
            cancel_requested_at=observation.cancel_requested_at,
            parked_at=observation.parked_at,
            query_count=observation.query_count,
            scanned_bytes=observation.scanned_bytes,
            engine_ms=observation.engine_ms,
            terminal_at=observation.terminal_at,
            athena_usage_state=observation.athena_usage_state,
            owner_disposition=observation.owner_disposition,
            usage_category=observation.usage_category,
            usage_authority_digest=observation.usage_authority_digest,
        )
    if state in {"intent", "submitted", "cancel_requested", "parked"}:
        return _parse_nonterminal(observation, core)
    raise TableAttemptError("attempt state is outside the closed vocabulary.")


@dataclass(frozen=True, slots=True)
class FenceRecord:
    """One read fence: its closed safety form beside its raw reconciliation snapshot."""

    fence: TableAttemptFence
    reconciliation: ReconciliationObservation


# --- Clause 4 recomputation hook ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecomputedAttemptIdentity:
    """The three identities clause 4 recomputes from the immutable fields."""

    attempt_id: str
    client_request_token: str
    envelope_digest: str


type AttemptIdentityRecomputation = Callable[[AttemptCore], RecomputedAttemptIdentity]


ATTEMPT_IDENTITY_KIND: Final = "stage1-table-attempt/v1"


@dataclass(frozen=True, slots=True)
class AttemptIdentityRequest:
    """The immutable §5 step-2 inputs to the one pinned attempt-identity constructor."""

    owner_kind: AttemptOwnerKind
    owner_ref: str
    physical_table_id: str
    lease_holder: str
    fencing_token: int
    ordinal: int
    start_envelope: str

    def __post_init__(self) -> None:
        _require_nonblank(self.owner_ref, "owner binding reference")
        _require_canonical_table(self.physical_table_id, "physical table id")
        _require_nonblank(self.lease_holder, "lease holder")
        if not _is_plain_int(self.fencing_token) or self.fencing_token < 1:
            raise TableAttemptError("fencing token must be a positive JSON integer.")
        if not _is_plain_int(self.ordinal) or self.ordinal < 1:
            raise TableAttemptError("ordinal must be a positive JSON integer.")
        _require_nonblank(self.start_envelope, "start envelope")


def attempt_identity(request: AttemptIdentityRequest) -> RecomputedAttemptIdentity:
    """Derive the pinned §5 step-2 attempt identity; no other derivation exists.

    ``attempt_id`` is the canonical authority digest of the identity tuple and is
    byte-identical to the Athena client request token; ``envelope_digest`` hashes
    the stored canonical start envelope's exact UTF-8 bytes.
    """
    identity_tuple = [
        ATTEMPT_IDENTITY_KIND,
        request.owner_kind.value,
        request.owner_ref,
        request.physical_table_id,
        request.lease_holder,
        request.fencing_token,
        request.ordinal,
    ]
    attempt_id = canonical_authority_digest(identity_tuple)
    envelope_digest = hashlib.sha256(request.start_envelope.encode("utf-8")).hexdigest()
    return RecomputedAttemptIdentity(
        attempt_id=attempt_id,
        client_request_token=attempt_id,
        envelope_digest=envelope_digest,
    )


def recompute_attempt_identity(core: AttemptCore) -> RecomputedAttemptIdentity:
    """Rebuild the identity from an attempt core: the production clause-4 recomputation."""
    if core.plan_unit_owner_ref is not None:
        owner_kind = AttemptOwnerKind.QUEUED_UNIT
        owner_ref = core.plan_unit_owner_ref
    else:
        owner_kind = AttemptOwnerKind.INLINE_RUN
        owner_ref = core.inline_run_owner_ref or ""
    return attempt_identity(
        AttemptIdentityRequest(
            owner_kind=owner_kind,
            owner_ref=owner_ref,
            physical_table_id=core.physical_table_id,
            lease_holder=core.lease_holder,
            fencing_token=core.fencing_token,
            ordinal=core.ordinal,
            start_envelope=core.start_envelope,
        )
    )


RESERVATION_IDENTITY_NAMESPACE: Final = "stage1-attempt-reservation/v1"


def attempt_reservation_identity(attempt_id: str) -> str:
    """Derive the pinned §5 step-2 permanent-slot reservation identity.

    Begin writes this value byte-identically to the attempt's ``reservation_identity``
    and the owner's ``reserved_slot_identity``; exact Begin replay reproduces the
    identical slot bytes. No other derivation, truncation, or prefix form exists.
    """
    _require_nonblank(attempt_id, "attempt id")
    return canonical_authority_digest([RESERVATION_IDENTITY_NAMESPACE, attempt_id])


# --- The §3.1 predicates --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CoreValidityRequest:
    """One pending-GSI candidate with its strongly read snapshots and identity hook."""

    table_id: str
    fence: FenceObservation | None
    attempt: AttemptObservation | None
    recompute_identity: AttemptIdentityRecomputation
    plan_unit_owner: PlanUnitOwnerObservation | None = None
    inline_run_owner: InlineRunOwnerObservation | None = None
    came_from_pending_gsi: bool = True
    projected_base_keys_resolve: bool = True


def _parsed_nonterminal_pair(
    request: CoreValidityRequest,
) -> tuple[ActiveFence | ParkedFence, NonterminalTableAttempt] | None:
    if not request.projected_base_keys_resolve or request.fence is None or request.attempt is None:
        return None
    try:
        expected_partition = table_attempt_partition_key(request.table_id)
        fence = parse_fence(request.fence)
        attempt = parse_attempt(request.attempt)
    except TableAttemptError:
        return None
    if isinstance(fence, ClearFence) or isinstance(attempt, TerminalAttempt):
        return None
    partitions_match = (
        request.fence.partition_key == expected_partition
        and request.attempt.partition_key == expected_partition
    )
    return (fence, attempt) if partitions_match else None


def _pair_bound(fence: ActiveFence | ParkedFence, attempt: NonterminalTableAttempt) -> bool:
    core = attempt.core
    bound = (
        fence.physical_table_id == core.physical_table_id
        and fence.attempt_id == core.attempt_id
        and fence.lease_holder == core.lease_holder
        and fence.fencing_token == core.fencing_token
    )
    if not bound:
        return False
    if isinstance(fence, ParkedFence):
        return isinstance(attempt, ParkedAttempt) and fence.parked_at == attempt.parked_at
    return isinstance(attempt, IntentAttempt | SubmittedAttempt | CancelRequestedAttempt)


def _identity_recomputes(request: CoreValidityRequest, core: AttemptCore) -> bool:
    try:
        recomputed = request.recompute_identity(core)
    except ValueError:
        return False
    return (
        recomputed.attempt_id == core.attempt_id
        and recomputed.client_request_token == core.client_request_token
        and recomputed.envelope_digest == core.envelope_digest
    )


def _count_in_range(value: int | None, low: int, high: int) -> bool:
    return value is not None and _is_plain_int(value) and low <= value <= high


def _plan_owner_valid(owner: PlanUnitOwnerObservation, core: AttemptCore) -> bool:
    return (
        owner.plan_partition_key == core.plan_unit_owner_ref
        and owner.unit_record_key == f"{UNIT_RECORD_PREFIX}{core.physical_table_id}"
        and owner.plan_meta_exists
        and owner.origin_intent_admitted
        and owner.unit_status == "writing"
        and owner.writer_execution_id == core.writer_execution_id
        and owner.ordinal == core.ordinal
        and owner.current_attempt_id == core.attempt_id
        and owner.lease_holder == core.lease_holder
        and owner.fencing_token == core.fencing_token
        and _count_in_range(owner.unit_reservation_count, 1, 10)
        and _count_in_range(owner.plan_reservation_count, 1, 32)
        and owner.reserved_slot_identity == core.reservation_identity
    )


def _inline_owner_valid(owner: InlineRunOwnerObservation, core: AttemptCore) -> bool:
    try:
        expected_record = run_record_key(owner.run_id)
    except TableAttemptError:
        return False
    return (
        owner.run_id == core.inline_run_owner_ref
        and owner.run_partition_key == f"{TABLE_ATTEMPT_PREFIX}{core.physical_table_id}"
        and owner.run_record_key == expected_record
        and owner.state in {"open", "cancel_requested", "reconciled"}
        and owner.writer_execution_id == core.writer_execution_id
        and owner.ordinal == core.ordinal
        and owner.current_attempt_id == core.attempt_id
        and owner.lease_holder == core.lease_holder
        and owner.fencing_token == core.fencing_token
        and _count_in_range(owner.reservation_count, 1, 10)
        and owner.reserved_slot_identity == core.reservation_identity
    )


def _owner_binding_valid(request: CoreValidityRequest, core: AttemptCore) -> bool:
    plan_owner = request.plan_unit_owner
    run_owner = request.inline_run_owner
    if (plan_owner is None) == (run_owner is None):
        return False
    if core.plan_unit_owner_ref is not None:
        return plan_owner is not None and _plan_owner_valid(plan_owner, core)
    return run_owner is not None and _inline_owner_valid(run_owner, core)


def core_valid(request: CoreValidityRequest) -> bool:
    """Whether the six §3.1 clauses all hold for one pending-GSI candidate pair."""
    pair = _parsed_nonterminal_pair(request)
    if pair is None:
        return False
    fence, attempt = pair
    return (
        _pair_bound(fence, attempt)
        and _identity_recomputes(request, attempt.core)
        and _owner_binding_valid(request, attempt.core)
    )


def core_invalid(request: CoreValidityRequest) -> bool:
    """Decide the closed complement of ``core_valid`` for a pending-GSI candidate."""
    return not core_valid(request)


_BLOCK_FIELD_NAMES: Final = (
    "reconcile_block_id",
    "reconcile_block_reason",
    "reconcile_blocked_at",
    "reconcile_observation_digest",
    "reconcile_recovery_ref",
)


def _requeue_audit_valid(rec: ReconciliationObservation) -> bool:
    ref = rec.requeued_from_case_ref
    instant = rec.requeued_at
    if ref is None and instant is None:
        return True
    return (
        isinstance(ref, str)
        and bool(_UUID4_TEXT.match(ref))
        and isinstance(instant, str)
        and is_canonical_instant(instant)
    )


def _pending_form_valid(rec: ReconciliationObservation, digest: str) -> bool:
    key = rec.reconcile_after_or_block
    count = rec.reconcile_attempt_count
    return (
        isinstance(key, str)
        and bool(_PENDING_KEY_TEXT.match(key))
        and key.endswith(f"#{digest}")
        and isinstance(count, int)
        and not isinstance(count, bool)
        and count >= 0
        and all(getattr(rec, name) is None for name in _BLOCK_FIELD_NAMES)
        and _requeue_audit_valid(rec)
    )


def _blocked_form_valid(rec: ReconciliationObservation) -> bool:
    ref = rec.reconcile_recovery_ref
    reason = rec.reconcile_block_reason
    blocked_at = rec.reconcile_blocked_at
    observation_digest = rec.reconcile_observation_digest
    block_id = rec.reconcile_block_id
    return (
        rec.reconcile_attempt_count is None
        and rec.requeued_from_case_ref is None
        and rec.requeued_at is None
        and isinstance(block_id, str)
        and bool(block_id.strip())
        and isinstance(reason, str)
        and reason in {member.value for member in ReconcileBlockReason}
        and isinstance(blocked_at, str)
        and is_canonical_instant(blocked_at)
        and isinstance(observation_digest, str)
        and bool(_DIGEST_TEXT.match(observation_digest))
        and isinstance(ref, str)
        and bool(_UUID4_TEXT.match(ref))
        and rec.reconcile_after_or_block == f"BLOCK#{ref}"
    )


def reconciliation_valid(observation: FenceObservation) -> bool:
    """Whether a core-valid pair's fence carries one canonical pending or blocked form."""
    try:
        fence = parse_fence(observation)
    except TableAttemptError:
        return False
    if isinstance(fence, ClearFence):
        return False
    rec = observation.reconciliation
    if rec.malformed_field_names:
        return False
    digest = binding_digest(
        BindingDigestRequest(
            physical_table_id=fence.physical_table_id,
            attempt_id=fence.attempt_id,
            lease_holder=fence.lease_holder,
            fencing_token=fence.fencing_token,
        )
    )
    if rec.reconcile_status == "pending":
        return _pending_form_valid(rec, digest)
    if rec.reconcile_status == "blocked":
        return _blocked_form_valid(rec)
    return False


def repairable_reconciliation_metadata(request: CoreValidityRequest) -> bool:
    """Whether only the reconciliation field set is malformed on a core-valid candidate."""
    return (
        request.came_from_pending_gsi
        and request.projected_base_keys_resolve
        and request.fence is not None
        and core_valid(request)
        and not reconciliation_valid(request.fence)
    )
