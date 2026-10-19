# SPDX-License-Identifier: MIT-0
"""Outbound ports for retained ADR-075 Stage-1 operational-status authority."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from domain.operations.stage1_operational_status import (
    BlockStage1OutboxRequest,
    MarkStage1OutboxDeliveredRequest,
    ProtectedStage1OutboxCase,
    RecordStage1AuthorityObservationRequest,
    RecordStage1OutboxDeliveryFailureRequest,
    RequeueStage1OutboxRequest,
    SendStage1StatusMessageRequest,
    Stage1AuthorityMember,
    Stage1OutboxAuthoritySnapshot,
    Stage1OutboxRecoveryInvocation,
    Stage1OutboxRecoveryResult,
    VerifiedStage1EventAuthority,
)


@dataclass(frozen=True, slots=True)
class Stage1OutboxKey:
    """One exact publications-table authority key discovered without payload."""

    partition_key: str
    record_key: str

    def __post_init__(self) -> None:
        if not self.partition_key.strip() or not self.record_key.strip():
            raise ValueError("Stage-1 outbox keys must be nonblank")


@dataclass(frozen=True, slots=True)
class Stage1PendingPage:
    """One bounded pending GSI page with no continuation cursor."""

    keys: tuple[Stage1OutboxKey, ...]


@dataclass(frozen=True, slots=True)
class Stage1RetainedAuthority:
    """Strongly read delivery state plus one validated retained authority member."""

    snapshot: Stage1OutboxAuthoritySnapshot
    member: Stage1AuthorityMember


@runtime_checkable
class Stage1OperationalStatusQueuePort(Protocol):
    """Closed FIFO transport for one validated Stage-1 queue member."""

    def send(self, request: SendStage1StatusMessageRequest) -> None:
        """Send one member with its derived group and deduplication identities."""
        ...


@runtime_checkable
class Stage1OutboxDeliveryStatePort(Protocol):
    """Typed retained-authority delivery-state operations."""

    def snapshot(self, partition_key: str, record_key: str) -> Stage1OutboxAuthoritySnapshot | None:
        """Strongly read delivery state without interpreting an invalid payload."""
        ...

    def authority(self, partition_key: str, record_key: str) -> Stage1RetainedAuthority | None:
        """Strongly read and validate one exact retained authority."""
        ...

    def pending(self, limit: int) -> Stage1PendingPage:
        """Query the fixed sparse pending index from its beginning with no cursor."""
        ...

    def record_observation(
        self, request: RecordStage1AuthorityObservationRequest
    ) -> Stage1OutboxAuthoritySnapshot:
        """Persist or re-read the first lifecycle observation instant."""
        ...

    def mark_delivered(
        self, request: MarkStage1OutboxDeliveredRequest
    ) -> Stage1OutboxAuthoritySnapshot:
        """Conditionally mark send-complete authority delivered."""
        ...

    def record_failure(
        self, request: RecordStage1OutboxDeliveryFailureRequest
    ) -> Stage1OutboxAuthoritySnapshot:
        """Record one below-threshold dependency failure."""
        ...

    def block(self, request: BlockStage1OutboxRequest) -> ProtectedStage1OutboxCase:
        """Atomically block poison authority and create its protected case."""
        ...

    def requeue(self, request: RequeueStage1OutboxRequest) -> Stage1OutboxAuthoritySnapshot:
        """Atomically requeue one server-resolved protected case."""
        ...


@runtime_checkable
class Stage1RetainedEventAuthorityPort(Protocol):
    """Strong wire-derived read of one retained Stage-1 lifecycle event authority."""

    def retained_event_authority(
        self, flow_id: str, opaque_digest: str
    ) -> VerifiedStage1EventAuthority | None:
        """Read the key derived from the received member alone; ``None`` when absent."""
        ...


@runtime_checkable
class Stage1OutboxRecoveryPort(Protocol):
    """Non-disclosing protected recovery invocation boundary."""

    def invoke(self, invocation: Stage1OutboxRecoveryInvocation) -> Stage1OutboxRecoveryResult:
        """Invoke recovery using only opaque case identity and sanitized reason."""
        ...
