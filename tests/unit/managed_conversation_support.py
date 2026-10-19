# SPDX-License-Identifier: MIT-0
"""Port doubles and a builder for driving the real `ManagedConversationRuntime` in unit tests."""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast

from adapters.inbound.conversation.block_runtime import (
    ManagedConversationControls,
    ManagedConversationDependencies,
    ManagedConversationRuntime,
)
from adapters.inbound.conversation.turns import VerifiedTurn, managed_request_from
from domain.conversation.audit import ConversationAuditRecordV6, TurnDeliveryRecordV1
from domain.conversation.managed import (
    HarnessTurnV2,
    ManagedConversationRequestV1,
    ManagedHarnessEventV1,
    ManagedHarnessTextDeltaV1,
    ManagedHarnessTurnEndedV1,
)
from ports.outbound.audit import AgentQualityAuditStorePort, AuditWriteDisposition, AuditWriteResult
from ports.outbound.evaluation_outbox import (
    EvaluationHandoffPort,
    EvaluationHandoffRequest,
    EvaluationOutboxWriteDisposition,
    EvaluationOutboxWriteResult,
)

__all__ = [
    "CORRELATION_ID",
    "NOW",
    "PRINCIPAL",
    "PROMPT_DIGEST",
    "RecordingAudit",
    "RecordingHandoff",
    "ScriptedHarness",
    "managed_request",
    "managed_runtime",
]

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
CORRELATION_ID = "123e4567-e89b-42d3-a456-426614174000"
PRINCIPAL = "5fdbe1d5-7901-4fee-9d1d-518103789c94"
"""A fictional verified Cognito `sub`; every terminal receipt must attribute the turn to it."""
PROMPT_DIGEST = "sha256:" + "a" * 64


class ScriptedHarness:
    """Replays scripted events lazily, noting each one just before it is produced.

    `produced` shows how far the runtime has pulled the Harness; `interrupted` shows the runtime
    closed the Harness stream while it was still suspended mid-turn.
    """

    def __init__(
        self,
        events: tuple[ManagedHarnessEventV1, ...],
        *,
        ends_turn: bool = True,
        failure: Exception | None = None,
    ) -> None:
        self.events = events
        self.ends_turn = ends_turn
        self.failure = failure
        self.calls: list[HarnessTurnV2] = []
        self.produced: list[str] = []
        self.finished = False
        self.interrupted = False

    def stream(self, turn: HarnessTurnV2) -> Generator[ManagedHarnessEventV1]:
        self.calls.append(turn)
        try:
            for event in self.events:
                self.produced.append(_label(event))
                yield event
            if self.failure is not None:
                raise self.failure
            if self.ends_turn:
                self.produced.append("end")
                yield ManagedHarnessTurnEndedV1()
        except GeneratorExit:
            self.interrupted = True
            raise
        finally:
            self.finished = True


def _label(event: ManagedHarnessEventV1) -> str:
    if isinstance(event, ManagedHarnessTextDeltaV1):
        return f"text:{event.text}"
    return type(event).__name__


class RecordingAudit:
    def __init__(self) -> None:
        self.records: list[ConversationAuditRecordV6] = []
        self.deliveries: list[TurnDeliveryRecordV1] = []
        self.delivery_receipt_times: list[str] = []

    def create(self, record: ConversationAuditRecordV6) -> AuditWriteResult:
        self.records.append(record)
        return AuditWriteResult(disposition=AuditWriteDisposition.APPLIED)

    def record_delivery(
        self, record: TurnDeliveryRecordV1, receipt_created_at: str
    ) -> AuditWriteResult:
        self.deliveries.append(record)
        self.delivery_receipt_times.append(receipt_created_at)
        return AuditWriteResult(disposition=AuditWriteDisposition.APPLIED)


class RecordingHandoff:
    def __init__(
        self,
        disposition: EvaluationOutboxWriteDisposition = EvaluationOutboxWriteDisposition.APPLIED,
        *,
        failure: Exception | None = None,
    ) -> None:
        self.requests: list[EvaluationHandoffRequest] = []
        self.disposition = disposition
        self.failure = failure

    def persist(self, request: EvaluationHandoffRequest) -> EvaluationOutboxWriteResult:
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        return EvaluationOutboxWriteResult(
            disposition=self.disposition,
            delivery_ref=request.outbox.envelope.delivery_ref,
        )


@dataclass
class _References:
    """Deterministic opaque references, issued in order."""

    issued: list[str] = field(default_factory=lambda: [])

    def __call__(self) -> str:
        self.issued.append(f"ref-{len(self.issued) + 1:02d}")
        return self.issued[-1]


def managed_request() -> ManagedConversationRequestV1:
    """One verified turn from the fictional principal."""
    return managed_request_from(
        VerifiedTurn(
            question="How did ROAS change last week?",
            verified_owner_id=PRINCIPAL,
            conversation_id="conversation-verified-owner-browser",
            custody_session_id="browser-session",
            correlation_id=CORRELATION_ID,
        )
    )


def managed_runtime(
    harness: ScriptedHarness,
    audit: RecordingAudit,
    handoff: RecordingHandoff | None = None,
) -> ManagedConversationRuntime:
    """Build the real runtime over scripted ports, with deterministic references and clock."""
    return ManagedConversationRuntime(
        ManagedConversationDependencies(
            harness=harness,
            audit_store=cast(AgentQualityAuditStorePort, audit),
            evaluation_handoff=None if handoff is None else cast(EvaluationHandoffPort, handoff),
        ),
        ManagedConversationControls(
            issue_reference=_References(),
            now=lambda: NOW,
            prompt_digest=PROMPT_DIGEST,
            guardrail_version="7",
        ),
    )
