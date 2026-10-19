# SPDX-License-Identifier: MIT-0
"""Immutable content-bearing records for durable released-response evaluation delivery."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from domain.evaluation.envelope import EvaluationEnvelope

_OPAQUE_REFERENCE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _opaque(value: str, label: str) -> None:
    if not _OPAQUE_REFERENCE.fullmatch(value):
        raise ValueError(f"{label} must be an opaque bounded reference.")


def _instant(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{label} must be an ISO instant.") from None
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError(f"{label} must be a UTC ISO instant.")
    return parsed


def evaluation_delivery_ref(audit_id: str, admission_ref: str) -> str:
    """Derive the stable delivery identity from content-free custody references."""
    _opaque(audit_id, "evaluation delivery audit ID")
    _opaque(admission_ref, "evaluation delivery admission reference")
    material = f"{audit_id}\n{admission_ref}".encode()
    return f"evaluation-delivery:{hashlib.sha256(material).hexdigest()}"


class EvaluationOutboxDeliveryStateV1(StrEnum):
    """Closed delivery lifecycle states for one protected evaluation envelope."""

    PENDING = "pending"
    LEASED = "leased"
    DELIVERED = "delivered"
    NOT_DISPATCHED = "not_dispatched"
    """Terminal: the dispatcher decided not to send this row (ADR-079); never re-sent."""


_STATE_SHAPES: dict[EvaluationOutboxDeliveryStateV1, tuple[tuple[bool, bool, bool], str]] = {
    # (lease, delivery time, decision time) each state holds, and the refusal when it does not.
    EvaluationOutboxDeliveryStateV1.PENDING: (
        (False, False, False),
        "pending evaluation outbox state cannot have a lease or terminal time.",
    ),
    EvaluationOutboxDeliveryStateV1.LEASED: (
        (True, False, False),
        "leased evaluation outbox state requires only its active lease.",
    ),
    EvaluationOutboxDeliveryStateV1.DELIVERED: (
        (False, True, False),
        "delivered evaluation outbox state requires only a delivery time.",
    ),
    EvaluationOutboxDeliveryStateV1.NOT_DISPATCHED: (
        (False, False, True),
        "not-dispatched evaluation outbox state requires only its decision time.",
    ),
}


@dataclass(frozen=True, slots=True, kw_only=True)
class EvaluationOutboxEnvelopeV1:
    """The sole durable record family permitted to retain evaluation content."""

    schema_version: Literal["EvaluationOutboxEnvelope.v1"] = "EvaluationOutboxEnvelope.v1"
    audit_id: str
    admission_ref: str
    delivery_ref: str
    envelope: EvaluationEnvelope
    admitted_at: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.audit_id, "evaluation outbox audit ID"),
            (self.admission_ref, "evaluation outbox admission reference"),
            (self.delivery_ref, "evaluation outbox delivery reference"),
        ):
            _opaque(value, label)
        if self.delivery_ref != evaluation_delivery_ref(self.audit_id, self.admission_ref):
            raise ValueError("evaluation outbox delivery reference must be deterministic.")
        if self.envelope.audit_id != self.audit_id:
            raise ValueError(
                "evaluation outbox envelope audit ID must match its custody reference."
            )
        if self.envelope.evaluation_admission_ref != self.admission_ref:
            raise ValueError(
                "evaluation outbox envelope admission reference must match its custody reference."
            )
        _instant(self.admitted_at, "evaluation outbox admission time")


@dataclass(frozen=True, slots=True, kw_only=True)
class EvaluationOutboxLeaseV1:
    """One immutable custody lease held while an evaluator delivery is attempted."""

    schema_version: Literal["EvaluationOutboxLease.v1"] = "EvaluationOutboxLease.v1"
    audit_id: str
    admission_ref: str
    delivery_ref: str
    lease_ref: str
    acquired_at: str
    expires_at: str
    attempt: int

    def __post_init__(self) -> None:
        for value, label in (
            (self.audit_id, "evaluation outbox lease audit ID"),
            (self.admission_ref, "evaluation outbox lease admission reference"),
            (self.delivery_ref, "evaluation outbox lease delivery reference"),
            (self.lease_ref, "evaluation outbox lease reference"),
        ):
            _opaque(value, label)
        if self.delivery_ref != evaluation_delivery_ref(self.audit_id, self.admission_ref):
            raise ValueError("evaluation outbox lease delivery reference must be deterministic.")
        if self.attempt < 1:
            raise ValueError("evaluation outbox lease attempt must be positive.")
        acquired_at = _instant(self.acquired_at, "evaluation outbox lease acquisition time")
        expires_at = _instant(self.expires_at, "evaluation outbox lease expiry time")
        if expires_at <= acquired_at:
            raise ValueError("evaluation outbox lease must expire after it is acquired.")


@dataclass(frozen=True, slots=True, kw_only=True)
class EvaluationOutboxStateV1:
    """An immutable lifecycle snapshot for the one deterministic delivery reference."""

    schema_version: Literal["EvaluationOutboxState.v1"] = "EvaluationOutboxState.v1"
    audit_id: str
    admission_ref: str
    delivery_ref: str
    state: EvaluationOutboxDeliveryStateV1
    available_at: str
    attempt_count: int
    lease: EvaluationOutboxLeaseV1 | None = None
    delivered_at: str | None = None
    decided_at: str | None = None
    """When the dispatcher decided not to send the row; set only in `NOT_DISPATCHED`."""

    def __post_init__(self) -> None:
        for value, label in (
            (self.audit_id, "evaluation outbox state audit ID"),
            (self.admission_ref, "evaluation outbox state admission reference"),
            (self.delivery_ref, "evaluation outbox state delivery reference"),
        ):
            _opaque(value, label)
        if self.delivery_ref != evaluation_delivery_ref(self.audit_id, self.admission_ref):
            raise ValueError("evaluation outbox state delivery reference must be deterministic.")
        _instant(self.available_at, "evaluation outbox availability time")
        if self.attempt_count < 0:
            raise ValueError("evaluation outbox attempt count cannot be negative.")
        held = (self.lease is not None, self.delivered_at is not None, self.decided_at is not None)
        expected, message = _STATE_SHAPES[self.state]
        if held != expected:
            raise ValueError(message)
        if self.decided_at is not None:
            _instant(self.decided_at, "evaluation outbox decision time")
        if self.lease is not None:
            if (
                self.lease.audit_id != self.audit_id
                or self.lease.admission_ref != self.admission_ref
                or self.lease.delivery_ref != self.delivery_ref
            ):
                raise ValueError("evaluation outbox lease must share the state custody references.")
            if self.lease.attempt != self.attempt_count:
                raise ValueError(
                    "evaluation outbox lease attempt must equal the state attempt count."
                )
        if self.delivered_at is not None:
            _instant(self.delivered_at, "evaluation outbox delivery time")


@dataclass(frozen=True, slots=True, kw_only=True)
class EvaluationOutboxInitialRecordV1:
    """The exact initial envelope and pending state persisted under one admission identity."""

    schema_version: Literal["EvaluationOutboxInitialRecord.v1"] = "EvaluationOutboxInitialRecord.v1"
    envelope: EvaluationOutboxEnvelopeV1
    state: EvaluationOutboxStateV1

    def __post_init__(self) -> None:
        if self.state.state is not EvaluationOutboxDeliveryStateV1.PENDING:
            raise ValueError("an initial evaluation outbox record must be pending.")
        if self.state.attempt_count != 0:
            raise ValueError("an initial evaluation outbox record must have no delivery attempts.")
        if (
            self.envelope.audit_id != self.state.audit_id
            or self.envelope.admission_ref != self.state.admission_ref
            or self.envelope.delivery_ref != self.state.delivery_ref
        ):
            raise ValueError("evaluation outbox initial records must share custody references.")
