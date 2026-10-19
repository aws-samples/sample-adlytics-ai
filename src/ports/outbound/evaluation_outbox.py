# SPDX-License-Identifier: MIT-0
"""Technology-neutral durable evaluation handoff and dispatch persistence interfaces."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from domain.conversation.audit import ConversationAuditRecordV6
from domain.evaluation.linkage import (
    EvaluationAdmissionV1,
    EvaluationDeliveryFamilyV1,
    EvaluationNotDispatchedReasonV1,
)
from domain.evaluation.outbox import (
    EvaluationOutboxEnvelopeV1,
    EvaluationOutboxInitialRecordV1,
    EvaluationOutboxStateV1,
)

_MAX_DUE_ITEMS = 25


class EvaluationOutboxWriteDisposition(StrEnum):
    """Closed result of idempotently creating one durable evaluation handoff."""

    APPLIED = "applied"
    EXACT_REPLAY = "exact_replay"


@dataclass(frozen=True, slots=True, kw_only=True)
class EvaluationOutboxWriteResult:
    """Result of the atomic audit, admission, and outbox write."""

    disposition: EvaluationOutboxWriteDisposition
    delivery_ref: str


@dataclass(frozen=True, slots=True, kw_only=True)
class EvaluationHandoffRequest:
    """Three records that become durable atomically before public terminal release."""

    audit: ConversationAuditRecordV6
    admission: EvaluationAdmissionV1
    outbox: EvaluationOutboxInitialRecordV1

    def __post_init__(self) -> None:
        if self.audit.audit_id != self.admission.audit_id:
            raise ValueError("evaluation handoff audit and admission identities must match.")
        if self.audit.evaluation_admission_ref != self.admission.admission_ref:
            raise ValueError("evaluation handoff admission must match the audit selection.")
        if (
            self.outbox.envelope.audit_id != self.audit.audit_id
            or self.outbox.envelope.admission_ref != self.admission.admission_ref
        ):
            raise ValueError("evaluation handoff outbox custody must match its audit admission.")
        # The dispatcher and the evaluation writer never read receipts: each takes its record's
        # expiry from the row's admission time, which is therefore the receipt's creation time.
        if self.outbox.envelope.admitted_at != self.audit.created_at:
            raise ValueError("evaluation handoff outbox admission time must be the receipt's.")


@dataclass(frozen=True, slots=True, kw_only=True)
class EvaluationOutboxDueRequest:
    """Bounded due-work query through the content-free ready index."""

    ready_at_or_before: str
    limit: int = 10

    def __post_init__(self) -> None:
        if not self.ready_at_or_before.strip() or not 1 <= self.limit <= _MAX_DUE_ITEMS:
            raise ValueError("evaluation outbox due request is invalid.")


@dataclass(frozen=True, slots=True, kw_only=True)
class EvaluationOutboxClaimRequest:
    """Conditional acquisition of one pending or expired evaluation delivery."""

    admission_ref: str
    lease_ref: str
    acquired_at: str
    expires_at: str


@dataclass(frozen=True, slots=True, kw_only=True)
class EvaluationOutboxReleaseRequest:
    """Conditional retry scheduling by the dispatcher that owns the lease."""

    admission_ref: str
    lease_ref: str
    available_at: str
    count_attempt: bool = True
    """False returns the claim's attempt: a row waiting for its delivery companion was not
    attempted, so its wait never lengthens a later enqueue failure's backoff."""


@dataclass(frozen=True, slots=True, kw_only=True)
class EvaluationOutboxDeliverRequest:
    """Conditional delivery finalization and its dispatched receipt (ADR-079).

    `families` is every enabled family the row was sent to at this run, with the evaluator set
    released for each; the receipt records exactly that set.
    """

    admission_ref: str
    lease_ref: str
    decided_at: str
    expires_at_epoch: int
    families: tuple[EvaluationDeliveryFamilyV1, ...]

    def __post_init__(self) -> None:
        if self.expires_at_epoch < 1:
            raise ValueError("evaluation outbox delivered retention must be a positive epoch.")
        if not self.families:
            raise ValueError("an evaluation outbox delivery names at least one family.")


@dataclass(frozen=True, slots=True, kw_only=True)
class EvaluationOutboxWithholdRequest:
    """Conditional terminal not-dispatched finalization and its receipt (ADR-079)."""

    admission_ref: str
    lease_ref: str
    decided_at: str
    reason: EvaluationNotDispatchedReasonV1
    expires_at_epoch: int

    def __post_init__(self) -> None:
        if self.expires_at_epoch < 1:
            raise ValueError("evaluation outbox withheld retention must be a positive epoch.")


@dataclass(frozen=True, slots=True, kw_only=True)
class EvaluationOutboxWorkItem:
    """Exact content-bearing envelope plus current delivery state for one claimed item."""

    envelope: EvaluationOutboxEnvelopeV1
    state: EvaluationOutboxStateV1


@runtime_checkable
class EvaluationHandoffPort(Protocol):
    """Atomically persist the request audit, admission, and content-bearing outbox record."""

    def persist(self, request: EvaluationHandoffRequest) -> EvaluationOutboxWriteResult:
        """Create all three records or attest their exact replay."""
        ...


@runtime_checkable
class EvaluationOutboxDispatchPort(Protocol):
    """Query, lease, retry, and finalize durable evaluation work."""

    def due(self, request: EvaluationOutboxDueRequest) -> tuple[str, ...]:
        """Return bounded due admission references from the ready index."""
        ...

    def claim(self, request: EvaluationOutboxClaimRequest) -> EvaluationOutboxWorkItem | None:
        """Claim one due item, returning none when another dispatcher won."""
        ...

    def release(self, request: EvaluationOutboxReleaseRequest) -> None:
        """Return one owned lease to pending with its next availability."""
        ...

    def deliver(self, request: EvaluationOutboxDeliverRequest) -> None:
        """Atomically mark delivered and write the immutable dispatched receipt."""
        ...

    def withhold(self, request: EvaluationOutboxWithholdRequest) -> None:
        """Atomically mark not dispatched and write the immutable not-dispatched receipt."""
        ...
