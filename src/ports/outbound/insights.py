# SPDX-License-Identifier: MIT-0
"""Technology-neutral cache, prompt-activation, and direct narration ports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from domain.presentation.insights import (
    InsightCacheKey,
    InsightCacheWriteResult,
    InsightCalibrationResult,
    InsightClaimedPromptCalibrationRequest,
    InsightContinuationRequest,
    InsightDeepAnalysis,
    InsightDeepCacheWriteResult,
    InsightDeepRecord,
    InsightGeneratedNarration,
    InsightGenerationRequest,
    InsightPendingPromptVersion,
    InsightPromptActivation,
    InsightPromptActivationRequest,
    InsightPromptCalibrationClaimRequest,
    InsightPromptCalibrationClaimResult,
    InsightPromptCalibrationOutboxWork,
    InsightPromptCalibrationRequest,
    InsightPromptCalibrationRunRequest,
    InsightPromptCalibrationWorkState,
    InsightPromptRetirementRequest,
    InsightPromptSlotState,
    InsightPromptTier,
    InsightPromptVersionCreateRequest,
    InsightPromptVersionRecord,
    InsightSlot,
    InsightSlotId,
)


@dataclass(frozen=True, slots=True)
class InsightCacheSupersessionRequest:
    """Mark one exact obsolete cache entry for TTL after its successor is known."""

    stale_key: InsightCacheKey
    successor_key: InsightCacheKey
    superseded_at: int

    def __post_init__(self) -> None:
        if type(self.superseded_at) is not int or self.superseded_at <= 0:
            raise ValueError(
                "insight cache supersession TTL must be a positive integer epoch second."
            )
        stale = self.stale_key
        successor = self.successor_key
        if (
            stale.analytical_scope_id,
            stale.subject,
            stale.window_start,
            stale.window_end,
            stale.slot_id,
            stale.tier,
            stale.filter_id,
        ) != (
            successor.analytical_scope_id,
            successor.subject,
            successor.window_start,
            successor.window_end,
            successor.slot_id,
            successor.tier,
            successor.filter_id,
        ):
            raise ValueError("insight cache supersession must retain one exact logical slot.")
        if stale.content_key == successor.content_key:
            raise ValueError("insight cache supersession requires a distinct successor key.")


@dataclass(frozen=True, slots=True)
class InsightCacheSupersessionResult:
    """Typed outcome of one conditional exact-key cache supersession."""

    superseded: bool


@runtime_checkable
class InsightCachePort(Protocol):
    """Read and conditionally create immutable owner-scoped derived insight entries."""

    def get(self, key: InsightCacheKey) -> InsightSlot | None:
        """Return an exact cache entry, or none when this immutable identity is absent."""
        ...

    def put_if_absent(self, slot: InsightSlot, key: InsightCacheKey) -> InsightCacheWriteResult:
        """Create one exact entry without replacing a concurrent producer's result."""
        ...


@runtime_checkable
class InsightDeepCachePort(Protocol):
    """Read and conditionally create immutable deep-tier analysis records (ADR-076)."""

    def get_deep(self, key: InsightCacheKey) -> InsightDeepRecord | None:
        """Return the exact deep record, or none when this tier-bearing identity is absent."""
        ...

    def put_deep_if_absent(
        self, record: InsightDeepRecord, key: InsightCacheKey
    ) -> InsightDeepCacheWriteResult:
        """Create one exact deep entry; an unavailable placeholder may be replaced by a fill."""
        ...


@runtime_checkable
class InsightCacheSupersessionPort(Protocol):
    """Conditionally expire one known obsolete cache key after a successor is current."""

    def supersede(self, request: InsightCacheSupersessionRequest) -> InsightCacheSupersessionResult:
        """Conditionally add TTL to one exact obsolete entry, never its successor."""
        ...


@runtime_checkable
class InsightPromptActivationPort(Protocol):
    """Resolve the active immutable prompt for one declared model-authored slot."""

    def active(
        self, slot_id: InsightSlotId, tier: InsightPromptTier
    ) -> InsightPromptActivation | None:
        """Return the tier's active pointer, or none when generation must remain unavailable."""
        ...


@runtime_checkable
class InsightPromptCalibrationRunner(Protocol):
    """Execute and evaluate every contract-owned fictional case for a pending version."""

    def run(self, request: InsightPromptCalibrationRunRequest) -> InsightCalibrationResult:
        """Return one complete server-derived calibration result or raise when unavailable."""
        ...


@runtime_checkable
class InsightPromptRegistryPort(InsightPromptActivationPort, Protocol):
    """Persist immutable prompt versions, calibration results, and lifecycle transitions."""

    def create_version(self, request: InsightPromptVersionCreateRequest) -> int:
        """Reserve the next immutable prompt version."""
        ...

    def pending_version(self, slot_id: InsightSlotId, version: int) -> InsightPendingPromptVersion:
        """Read the exact pending immutable prompt and allowed route for server-run calibration."""
        ...

    def pending_calibration_outbox(
        self, slot_ids: tuple[InsightSlotId, ...]
    ) -> tuple[InsightPromptCalibrationOutboxWork, ...]:
        """Read retained work identities not yet accepted by SQS."""
        ...

    def acknowledge_calibration_outbox(self, slot_id: InsightSlotId, version: int) -> None:
        """Conditionally remove work only after SQS accepted its identity."""
        ...

    def calibration_work_state(
        self, slot_id: InsightSlotId, version: int, now_epoch_seconds: int
    ) -> InsightPromptCalibrationWorkState:
        """Read terminal/pending and whether an unexpired worker lease exists."""
        ...

    def claim_calibration(
        self, request: InsightPromptCalibrationClaimRequest
    ) -> InsightPromptCalibrationClaimResult:
        """Conditionally lease pending work; duplicates observe active or terminal state."""
        ...

    def complete_claimed_calibration(self, request: InsightClaimedPromptCalibrationRequest) -> None:
        """Persist a complete result only under the worker's exact lease."""
        ...

    def record_calibration(self, request: InsightPromptCalibrationRequest) -> None:
        """Persist one complete immutable calibration result."""
        ...

    def activate(self, request: InsightPromptActivationRequest) -> None:
        """Move the active pointer only when the stored result passed."""
        ...

    def retire(self, request: InsightPromptRetirementRequest) -> None:
        """Conditionally remove the active pointer while retaining an event."""
        ...

    def slot_states(
        self, slot_ids: tuple[InsightSlotId, ...]
    ) -> tuple[InsightPromptSlotState, ...]:
        """Read retained pointer and newest-version state for declared slots only."""
        ...

    def versions(self, slot_id: InsightSlotId) -> tuple[InsightPromptVersionRecord, ...]:
        """Read one slot's immutable versions newest first with calibration cases."""
        ...

    def version(self, slot_id: InsightSlotId, version: str) -> InsightPromptActivation | None:
        """Read one immutable version by its exact prompt identity for durable generation."""
        ...


class InsightPromptCalibrationQueuePort(Protocol):
    """Durably admit one immutable prompt-version identity for calibration."""

    def enqueue(self, slot_id: InsightSlotId, version: int) -> str:
        """Return the queue message identity after durable acceptance."""
        ...


@runtime_checkable
class InsightContinuationPort(Protocol):
    """Durably admit an exact server-derived cache miss for later processing."""

    def enqueue(self, request: InsightContinuationRequest) -> str:
        """Persist work before the interactive response declares the slot unavailable."""
        ...


@runtime_checkable
class InsightGenerationPort(Protocol):
    """Make exactly one direct structured generation request for one slot."""

    def generate(self, request: InsightGenerationRequest) -> InsightGeneratedNarration:
        """Return the decoded structured narration; provider failures raise a dependency error.

        The checks are structural (ADR-082 §3): a malformed, truncated, or
        wrong-output-class response raises an invariant error. The narration's
        figures and wording are governed by its prompt and measured by calibration
        and evaluation; nothing here attests them.
        """
        ...

    def generate_deep(self, request: InsightGenerationRequest) -> InsightDeepAnalysis:
        """Return one decoded deep-dive analysis for a deep-tier request (ADR-076 decision 1).

        The request's cache key must carry the deep tier; the same structural checks
        the card tier applies decide acceptance.
        """
        ...
