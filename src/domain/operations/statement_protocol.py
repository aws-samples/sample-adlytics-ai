# SPDX-License-Identifier: MIT-0
"""Pure ADR-075 §5/§6 statement protocol: deadlines, the step-2 validator, consolidation.

Everything here is deterministic and clock-free. The §5 step-1 deadline derivation
takes one already-captured monotonic instant and remaining budget in integer
milliseconds and returns either the three derived deadlines or the typed retryable
insufficient-budget refusal — it never reads a clock. The §5 step-2 closed local
typed validator checks the statement kind, owner/lease tuple form, canonical start
envelope and its §11 byte bound, client request token, parameter types, and rendered
SQL, returning a typed local refusal that starts no attempt and reserves
nothing. The §6 machinery owns exact-object deduplication, canonical object order,
the clean per-unit statement-kind sequence (merges then exactly one verification),
and nothing that renders SQL — composition is an adapter's job.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from domain.operations.table_attempt import (
    AttemptIdentityRequest,
    AttemptOwnerKind,
    RecomputedAttemptIdentity,
    TableAttemptError,
    attempt_identity,
    attempt_reservation_identity,
    is_canonical_table_id,
)
from domain.operations.write_plan import MAX_ENCODED_IDENTITY_BYTES, CanonicalObjectEntry

__all__ = [
    "CANCELLATION_RESERVE_MS",
    "MAX_ACCEPTED_ATTEMPTS_PER_UNIT",
    "MAX_STATEMENT_BYTES",
    "MIN_EXCLUSIVE_BUDGET_MS",
    "SAFETY_RESERVE_MS",
    "AcceptedStatementStart",
    "CanonicalObjectCandidate",
    "InsufficientStatementBudget",
    "LocalStatementRefusal",
    "StatementBudgetResult",
    "StatementDeadlines",
    "StatementKind",
    "StatementProtocolError",
    "StatementRefusalReason",
    "StatementStartBounds",
    "StatementStartValidation",
    "ValidateStatementStartRequest",
    "consolidate_canonical_objects",
    "derive_statement_deadlines",
    "unit_statement_kinds",
    "validate_statement_start",
]

# §5 step 1: S = I - 60000 and Q = S - 60000; r0 must strictly exceed 120000 ms.
SAFETY_RESERVE_MS: Final = 60_000
CANCELLATION_RESERVE_MS: Final = 60_000
MIN_EXCLUSIVE_BUDGET_MS: Final = SAFETY_RESERVE_MS + CANCELLATION_RESERVE_MS

# §11 `athenaMaxStatementBytes`: the reviewed bound on one statement's canonical start
# envelope (SQL text plus every encoded parameter), in UTF-8 bytes. Adapters resolving
# `AppConfig.ingestion` pass the same value through `StatementStartBounds` and the §6
# packer rather than reading here. There is no parameter-count bound (§11 amendment,
# 2026-10-10): Athena documents none, and the envelope bytes bound the request and the
# persisted ATTEMPT item together.
MAX_STATEMENT_BYTES: Final = 200_000

# §11 `maxAcceptedAttemptsPerUnit`: every clean M(u)+1 must stay within 10.
MAX_ACCEPTED_ATTEMPTS_PER_UNIT: Final = 10

_HOLDER_TEXT: Final = re.compile(r"\A[0-9a-f]{64}\Z")


class StatementProtocolError(ValueError):
    """A statement-protocol input violates the closed §5/§6 contract."""


class StatementKind(StrEnum):
    """The closed Stage-1 writer statement vocabulary (§6: merge and verification)."""

    MERGE = "merge"
    VERIFICATION = "verification"


def _is_plain_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# --- §5 step 1: deadline and budget derivation ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class StatementDeadlines:
    """The three §5 step-1 deadlines derived from one monotonic capture, in ms.

    ``invocation_deadline_ms`` is ``I=m0+r0``, ``safety_deadline_ms`` is
    ``S=I-60000``, and ``cancellation_deadline_ms`` is ``Q=S-60000``. Every later
    comparison or sleep uses monotonic time only; no wall clock participates.
    """

    started_monotonic_ms: int
    invocation_deadline_ms: int
    safety_deadline_ms: int
    cancellation_deadline_ms: int


@dataclass(frozen=True, slots=True)
class InsufficientStatementBudget:
    """The typed retryable §5 refusal for ``r0<=120000``: no attempt starts."""

    remaining_budget_ms: int

    @property
    def retryable(self) -> bool:
        """Insufficient budget is always retryable on a later delivery."""
        return True


type StatementBudgetResult = StatementDeadlines | InsufficientStatementBudget


def derive_statement_deadlines(
    monotonic_now_ms: int, remaining_budget_ms: int
) -> StatementBudgetResult:
    """Derive ``I``/``S``/``Q`` from one captured ``(m0, r0)`` pair, integers only.

    ``r0<=120000`` starts no attempt and returns the typed retryable
    insufficient-budget result; ``r0=120001`` accepts with ``Q=m0+1``. A full
    900000 ms budget gives offsets ``Q/S/I=780000/840000/900000`` from ``m0``.
    """
    if not _is_plain_int(monotonic_now_ms):
        raise StatementProtocolError("monotonic capture must be an integer of milliseconds.")
    if not _is_plain_int(remaining_budget_ms) or remaining_budget_ms < 0:
        raise StatementProtocolError("remaining budget must be a nonnegative integer of ms.")
    if remaining_budget_ms <= MIN_EXCLUSIVE_BUDGET_MS:
        return InsufficientStatementBudget(remaining_budget_ms=remaining_budget_ms)
    invocation = monotonic_now_ms + remaining_budget_ms
    safety = invocation - SAFETY_RESERVE_MS
    cancellation = safety - CANCELLATION_RESERVE_MS
    return StatementDeadlines(
        started_monotonic_ms=monotonic_now_ms,
        invocation_deadline_ms=invocation,
        safety_deadline_ms=safety,
        cancellation_deadline_ms=cancellation,
    )


# --- §5 step 2: the closed local typed validator --------------------------------------------------


@dataclass(frozen=True, slots=True)
class StatementStartBounds:
    """The §11 bounds the step-2 validator enforces; defaults are the reviewed values."""

    max_statement_bytes: int = MAX_STATEMENT_BYTES
    max_encoded_identity_bytes: int = MAX_ENCODED_IDENTITY_BYTES

    def __post_init__(self) -> None:
        for name in ("max_statement_bytes", "max_encoded_identity_bytes"):
            value: int = getattr(self, name)
            if not _is_plain_int(value) or value < 1:
                raise StatementProtocolError(f"{name} must be a positive integer.")


@dataclass(frozen=True, slots=True)
class ValidateStatementStartRequest:
    """One statement's exact pre-Begin facts; the validator, not this form, refuses.

    ``client_request_token`` is optional: when supplied it must be byte-equal to
    the derived attempt id. Parameters are the exact ordered statement parameter
    list the renderer produced; ``rendered_sql`` is that renderer's final SQL text.
    """

    statement_kind: str
    owner_kind: str
    owner_ref: str
    physical_table_id: str
    lease_holder: str
    fencing_token: int
    ordinal: int
    start_envelope: str
    rendered_sql: str
    parameters: tuple[object, ...]
    client_request_token: str | None = None
    bounds: StatementStartBounds = field(default_factory=StatementStartBounds)


class StatementRefusalReason(StrEnum):
    """The closed local refusal vocabulary; each member names one failed check family."""

    STATEMENT_KIND = "statement_kind_outside_closed_vocabulary"
    OWNER_LEASE_TUPLE = "owner_lease_tuple_invalid"
    START_ENVELOPE = "start_envelope_invalid"
    CLIENT_REQUEST_TOKEN = "client_request_token_invalid"  # noqa: S105 — a refusal label, not a credential
    PARAMETERS = "parameters_invalid"
    RENDERED_SQL = "rendered_sql_invalid"


@dataclass(frozen=True, slots=True)
class LocalStatementRefusal:
    """The typed §5 step-2 local refusal: starts no attempt and reserves nothing."""

    reason: StatementRefusalReason
    description: str


@dataclass(frozen=True, slots=True)
class AcceptedStatementStart:
    """A validated statement start with its derived §5 step-2 identities."""

    statement_kind: StatementKind
    identity: RecomputedAttemptIdentity
    reservation_identity: str


type StatementStartValidation = AcceptedStatementStart | LocalStatementRefusal


def _owner_tuple_refusal(  # noqa: PLR0911 -- one typed return per closed tuple check
    request: ValidateStatementStartRequest,
) -> LocalStatementRefusal | None:
    if request.owner_kind not in {member.value for member in AttemptOwnerKind}:
        return LocalStatementRefusal(
            StatementRefusalReason.OWNER_LEASE_TUPLE,
            "owner kind is outside the closed queued_unit|inline_run vocabulary.",
        )
    if not request.owner_ref.strip():
        return LocalStatementRefusal(
            StatementRefusalReason.OWNER_LEASE_TUPLE,
            "owner binding reference must be nonblank text.",
        )
    if not is_canonical_table_id(request.physical_table_id):
        return LocalStatementRefusal(
            StatementRefusalReason.OWNER_LEASE_TUPLE,
            "physical table id must be a canonical database-qualified table id.",
        )
    if not _HOLDER_TEXT.match(request.lease_holder):
        return LocalStatementRefusal(
            StatementRefusalReason.OWNER_LEASE_TUPLE,
            "lease holder must be the 64-lowercase-hex §5 holder digest.",
        )
    if not _is_plain_int(request.fencing_token) or request.fencing_token < 1:
        return LocalStatementRefusal(
            StatementRefusalReason.OWNER_LEASE_TUPLE,
            "fencing token must be a positive JSON integer.",
        )
    if not _is_plain_int(request.ordinal) or request.ordinal < 1:
        return LocalStatementRefusal(
            StatementRefusalReason.OWNER_LEASE_TUPLE,
            "ordinal must be a positive JSON integer.",
        )
    return None


def _statement_body_refusal(
    request: ValidateStatementStartRequest,
) -> LocalStatementRefusal | None:
    if not request.start_envelope.strip():
        return LocalStatementRefusal(
            StatementRefusalReason.START_ENVELOPE,
            "canonical start envelope must be nonblank text.",
        )
    envelope_bytes = len(request.start_envelope.encode("utf-8"))
    if envelope_bytes > request.bounds.max_statement_bytes:
        return LocalStatementRefusal(
            StatementRefusalReason.START_ENVELOPE,
            f"the canonical start envelope is {envelope_bytes} UTF-8 bytes, beyond the "
            f"{request.bounds.max_statement_bytes}-byte §11 statement bound.",
        )
    if not request.rendered_sql.strip():
        return LocalStatementRefusal(
            StatementRefusalReason.RENDERED_SQL,
            "rendered SQL must be nonblank text.",
        )
    if any(not isinstance(parameter, str) for parameter in request.parameters):
        return LocalStatementRefusal(
            StatementRefusalReason.PARAMETERS,
            "every statement parameter must be text; the renderer emits only text.",
        )
    return None


def validate_statement_start(
    request: ValidateStatementStartRequest,
) -> StatementStartValidation:
    """Run the §5 step-2 closed local typed validator over one statement start.

    Any failure returns the typed local refusal — no attempt starts and nothing is
    reserved. Acceptance derives the pinned attempt identity, client request
    token, envelope digest, and permanent reservation identity; a supplied token
    that is not byte-equal to the derived attempt id refuses.
    """
    if request.statement_kind not in {member.value for member in StatementKind}:
        return LocalStatementRefusal(
            StatementRefusalReason.STATEMENT_KIND,
            "statement kind is outside the closed merge|verification vocabulary.",
        )
    refusal = _owner_tuple_refusal(request) or _statement_body_refusal(request)
    if refusal is not None:
        return refusal
    try:
        identity = attempt_identity(
            AttemptIdentityRequest(
                owner_kind=AttemptOwnerKind(request.owner_kind),
                owner_ref=request.owner_ref,
                physical_table_id=request.physical_table_id,
                lease_holder=request.lease_holder,
                fencing_token=request.fencing_token,
                ordinal=request.ordinal,
                start_envelope=request.start_envelope,
            )
        )
    except TableAttemptError as error:
        return LocalStatementRefusal(StatementRefusalReason.OWNER_LEASE_TUPLE, str(error))
    token = request.client_request_token
    if token is not None and token != identity.client_request_token:
        return LocalStatementRefusal(
            StatementRefusalReason.CLIENT_REQUEST_TOKEN,
            "the supplied client request token is not byte-equal to the derived attempt id.",
        )
    return AcceptedStatementStart(
        statement_kind=StatementKind(request.statement_kind),
        identity=identity,
        reservation_identity=attempt_reservation_identity(identity.attempt_id),
    )


# --- §6: exact-object deduplication and canonical object order ------------------------------------


@dataclass(frozen=True, slots=True)
class CanonicalObjectCandidate:
    """One candidate object beside its exact encoded object authority bytes."""

    entry: CanonicalObjectEntry
    encoded_object_authority: bytes

    def __post_init__(self) -> None:
        if not self.encoded_object_authority:
            raise StatementProtocolError("encoded object authority must be nonempty bytes.")


def consolidate_canonical_objects(
    candidates: tuple[CanonicalObjectCandidate, ...],
) -> tuple[CanonicalObjectEntry, ...]:
    """Deduplicate exact objects and return the §6 canonical object order.

    Two candidates sharing an object ``(kind, reference)`` key deduplicate only
    when their reference, checksum, publication identity, and encoded object
    authority are byte-identical — the entry and authority bytes must both agree;
    any difference refuses. The result is ascending unsigned UTF-8 byte
    lexicographic order of ``(kind, reference)``; caller order never participates.
    """
    retained: dict[tuple[bytes, bytes], CanonicalObjectCandidate] = {}
    for candidate in candidates:
        key = candidate.entry.object_key
        existing = retained.get(key)
        if existing is None:
            retained[key] = candidate
            continue
        identical = (
            existing.entry == candidate.entry
            and existing.encoded_object_authority == candidate.encoded_object_authority
        )
        if not identical:
            raise StatementProtocolError(
                "two objects share one (kind, reference) key without byte-identical "
                "checksum, publication identity, and encoded object authority."
            )
    return tuple(retained[key].entry for key in sorted(retained))


# --- §6/§2.1: the clean per-unit statement sequence -----------------------------------------------


def unit_statement_kinds(merge_count: int) -> tuple[StatementKind, ...]:
    """Return the clean §6 statement sequence: ``M(u)`` merges then one verification.

    A unit carries at least one object and every object supplies at least one
    record, so a clean unit runs at least one merge; ``M(u)+1`` must stay within
    the §11 10-accepted-attempt unit cap, so ``merge_count`` beyond 9 refuses.
    """
    if not _is_plain_int(merge_count) or merge_count < 1:
        raise StatementProtocolError("a clean unit runs at least one merge statement.")
    if merge_count + 1 > MAX_ACCEPTED_ATTEMPTS_PER_UNIT:
        raise StatementProtocolError(
            "a clean unit's merges plus one verification must stay within 10 attempts."
        )
    return (*(StatementKind.MERGE,) * merge_count, StatementKind.VERIFICATION)
