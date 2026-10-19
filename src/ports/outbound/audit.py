# SPDX-License-Identifier: MIT-0
"""Service-neutral persistence interfaces for minimized Agent Quality audit data."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol, cast, runtime_checkable

from domain.conversation.audit import (
    AuditAggregateMetricV2,
    AuditFindingV3,
    AuditTerminalOutcomeV2,
    ConversationAuditRecordV6,
    TurnDeliveryRecordV1,
    require_utc_instant,
)
from domain.evaluation.linkage import (
    EvaluationAdmissionV1,
    EvaluationDeliveryReceiptV2,
    EvaluationResultV3,
    EvaluationWriterReceiptV1,
)

__all__ = [
    "AgentQualityAggregatePort",
    "AgentQualityAuditStorePort",
    "AuditCorrelationRequest",
    "AuditFindingStorePort",
    "AuditFindingsReaderPort",
    "AuditWriteDisposition",
    "AuditWriteResult",
    "EvaluationDeliveryPredatesGate",
    "EvaluationDeliveryStorePort",
    "EvaluationResultLinkRequest",
    "EvaluationResultLinkResolution",
    "EvaluationResultLinkResolutionState",
    "EvaluationResultLinkResolverPort",
    "EvaluationResultLinkTarget",
    "EvaluationVerdictStorePort",
    "EvaluationWriterReceiptStorePort",
    "ReceiptTurnRow",
    "ReceiptTurnsPage",
    "ReceiptTurnsPageRequest",
    "StoredRecord",
    "TurnDeliveryReaderPort",
]
_CORRELATION_ID_LENGTH = 36
_CORRELATION_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")


MAX_TURNS_WINDOW = timedelta(days=31)
"""The longest creation-time window one `turns` listing may span."""
MAX_TURNS_PAGE_LIMIT = 100
DEFAULT_TURNS_PAGE_LIMIT = 50


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredRecord[T]:
    """One attested record together with the expiry read from its stored item (ADR-081 §5).

    `expires_at` is whole epoch seconds as stored, never recomputed by a reader; `None` only for
    an item written before retention existed and not yet backfilled.
    """

    record: T
    expires_at: int | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ReceiptTurnsPageRequest:
    """One bounded page of receipts by creation time, newest first (ADR-078 §5).

    The window is `window_from <= created_at < window_to`. An empty `terminal_outcomes` set
    applies no filter. `cursor` is the opaque value a previous page returned for this same window
    and filter set.
    """

    window_from: datetime
    window_to: datetime
    terminal_outcomes: frozenset[AuditTerminalOutcomeV2] = frozenset()
    limit: int = DEFAULT_TURNS_PAGE_LIMIT
    cursor: str | None = None

    def __post_init__(self) -> None:
        for bound in (self.window_from, self.window_to):
            if bound.tzinfo is None or bound.utcoffset() != UTC.utcoffset(bound):
                raise ValueError("turns window bounds must be UTC instants.")
        if self.window_from >= self.window_to:
            raise ValueError("turns window must start before it ends.")
        if self.window_to - self.window_from > MAX_TURNS_WINDOW:
            raise ValueError("turns window must span at most 31 days.")
        limit = cast("object", self.limit)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_TURNS_PAGE_LIMIT
        ):
            raise ValueError("turns limit must be a whole number from 1 to 100.")
        outcomes = cast("frozenset[object]", self.terminal_outcomes)
        if not all(isinstance(value, AuditTerminalOutcomeV2) for value in outcomes):
            raise ValueError("turns filter values must be terminal outcomes.")
        if self.cursor is not None and not self.cursor:
            raise ValueError("turns cursor must not be empty.")


@dataclass(frozen=True, slots=True, kw_only=True)
class ReceiptTurnRow:
    """One listed receipt: identity, creation, outcome, and expiry; never the principal."""

    audit_id: str
    correlation_id: str
    created_at: str
    terminal_outcome: AuditTerminalOutcomeV2
    expires_at: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ReceiptTurnsPage:
    """One page of rows and the cursor to the next page, `None` after the last."""

    turns: tuple[ReceiptTurnRow, ...]
    cursor: str | None


class AuditWriteDisposition(StrEnum):
    """Closed immutable-write result shared by receipts and derived records."""

    APPLIED = "applied"
    EXACT_REPLAY = "exact_replay"


@dataclass(frozen=True, slots=True, kw_only=True)
class AuditWriteResult:
    """Result of one immutable write."""

    disposition: AuditWriteDisposition


@dataclass(frozen=True, slots=True, kw_only=True)
class AuditCorrelationRequest:
    """A narrow normalized-correlation lookup; broad filtering is intentionally absent."""

    correlation_id: str

    def __post_init__(self) -> None:
        if len(self.correlation_id) != _CORRELATION_ID_LENGTH or not _CORRELATION_ID.fullmatch(
            self.correlation_id
        ):
            raise ValueError("audit correlation ID must be normalized.")


@dataclass(frozen=True, slots=True, kw_only=True)
class EvaluationResultLinkRequest:
    """The two transient references required to attest one service result in memory."""

    correlation_id: str
    trace_id: str

    def __post_init__(self) -> None:
        AuditCorrelationRequest(correlation_id=self.correlation_id)
        if not _TRACE_ID.fullmatch(self.trace_id):
            raise ValueError(
                "evaluation result link trace ID must be a lowercase 32-character hex ID."
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class EvaluationResultLinkTarget:
    """Opaque retained references safe to carry beyond protected ingress parsing."""

    audit_id: str
    admission_ref: str
    receipt_created_at: str
    """The linked receipt's creation time, from which the verdict's expiry is computed."""

    def __post_init__(self) -> None:
        if not self.audit_id or not self.admission_ref:
            raise ValueError("evaluation result link target references are required.")
        require_utc_instant(self.receipt_created_at, "linked receipt creation time")


class EvaluationResultLinkResolutionState(StrEnum):
    """Closed resolver outcomes that never expose trace/correlation values."""

    LINKED = "linked"
    PENDING = "pending"
    UNMATCHED = "unmatched"


@dataclass(frozen=True, slots=True, kw_only=True)
class EvaluationResultLinkResolution:
    """Bounded two-reference attestation outcome for one protected service event."""

    state: EvaluationResultLinkResolutionState
    target: EvaluationResultLinkTarget | None = None

    def __post_init__(self) -> None:
        has_target = self.target is not None
        if (self.state is EvaluationResultLinkResolutionState.LINKED) != has_target:
            raise ValueError("evaluation result link resolution target is inconsistent.")


@runtime_checkable
class EvaluationResultLinkResolverPort(Protocol):
    """Resolve one protected service result to an exact retained audit/writer pair."""

    def resolve_evaluation_result(
        self, request: EvaluationResultLinkRequest
    ) -> EvaluationResultLinkResolution:
        """Strongly attest one result without persisting its transient source references."""
        ...


@dataclass(frozen=True, slots=True, kw_only=True)
class EvaluationDeliveryPredatesGate:
    """A stored V1 delivery receipt: the turn was delivered before the gate existed (ADR-079).

    It is classified, never decoded into a V2 receipt, so it can name no family, evaluator, or
    provenance a verdict could be stamped from.
    """

    audit_id: str
    admission_ref: str

    def __post_init__(self) -> None:
        if not self.audit_id or not self.admission_ref:
            raise ValueError("a legacy evaluation delivery names its exact references.")


@runtime_checkable
class TurnDeliveryReaderPort(Protocol):
    """Read one turn's ADR-063 delivery companion; the dispatcher's only receipts-table read."""

    def get_delivery(self, audit_id: str) -> TurnDeliveryRecordV1 | None:
        """Strongly retrieve the delivery companion; absent reads as unknown (ADR-063)."""
        ...


@runtime_checkable
class AuditFindingsReaderPort(Protocol):
    """Read bounded immutable findings after an exact audit record is authorized."""

    def get_findings(self, audit_id: str) -> tuple[AuditFindingV3, ...]:
        """Strongly retrieve bounded immutable findings in one exact audit partition."""
        ...


@runtime_checkable
class AgentQualityAuditStorePort(Protocol):
    """The receipts family: immutable receipts, delivery dispositions, and admissions.

    Every other per-turn family has its own port and its own single writer (ADR-080 §1).
    """

    def create(self, record: ConversationAuditRecordV6) -> AuditWriteResult:
        """Write one immutable exact receipt."""
        ...

    def record_delivery(
        self, record: TurnDeliveryRecordV1, receipt_created_at: str
    ) -> AuditWriteResult:
        """Write one immutable post-stream delivery disposition companion (ADR-063).

        Its expiry is its receipt's creation time plus the declared retention (ADR-081 §5).
        """
        ...

    def get_delivery(self, audit_id: str) -> TurnDeliveryRecordV1 | None:
        """Strongly retrieve the delivery companion; absent reads as unknown (ADR-063)."""
        ...

    def get(self, audit_id: str) -> ConversationAuditRecordV6 | None:
        """Strongly retrieve and attest one receipt by its exact ID."""
        ...

    def discover(self, request: AuditCorrelationRequest) -> tuple[ConversationAuditRecordV6, ...]:
        """Query one normalized correlation and strongly attest each receipt."""
        ...

    def create_evaluation_admission(
        self, admission: EvaluationAdmissionV1, receipt_created_at: str
    ) -> AuditWriteResult:
        """Write one immutable selected protected-evaluation admission."""
        ...

    def get_evaluation_admission(
        self, audit_id: str, admission_ref: str
    ) -> EvaluationAdmissionV1 | None:
        """Strongly retrieve one exact admission."""
        ...


@runtime_checkable
class AuditFindingStorePort(AuditFindingsReaderPort, Protocol):
    """The findings family, written only by the agent-quality processor."""

    def write_finding(self, finding: AuditFindingV3, receipt_created_at: str) -> AuditWriteResult:
        """Write one immutable deterministic structural finding."""
        ...


@runtime_checkable
class EvaluationDeliveryStorePort(Protocol):
    """The evaluation delivery receipts family, written only by the outbox dispatcher."""

    def write_evaluation_delivery(
        self, delivery: EvaluationDeliveryReceiptV2, receipt_created_at: str
    ) -> AuditWriteResult:
        """Write one immutable dispatch decision receipt."""
        ...

    def get_evaluation_delivery(
        self, audit_id: str, admission_ref: str
    ) -> EvaluationDeliveryReceiptV2 | EvaluationDeliveryPredatesGate | None:
        """Strongly retrieve one exact receipt, classifying a V1 receipt as predating the gate."""
        ...


@runtime_checkable
class EvaluationWriterReceiptStorePort(Protocol):
    """The evaluation writer receipts family, written only by the evaluation writer."""

    def write_evaluation_writer_receipt(
        self, receipt: EvaluationWriterReceiptV1, receipt_created_at: str
    ) -> AuditWriteResult:
        """Write one immutable protected-span writer receipt."""
        ...

    def get_evaluation_writer_receipt(
        self, audit_id: str, admission_ref: str
    ) -> EvaluationWriterReceiptV1 | None:
        """Strongly retrieve one exact writer receipt."""
        ...


@runtime_checkable
class EvaluationVerdictStorePort(Protocol):
    """The evaluation verdicts family, written only by the result linker."""

    def write_evaluation_result(
        self, result: EvaluationResultV3, receipt_created_at: str
    ) -> AuditWriteResult:
        """Write one immutable allowlisted evaluator result with its provenance."""
        ...

    def get_evaluation_results(
        self, audit_id: str, admission_ref: str
    ) -> tuple[EvaluationResultV3, ...]:
        """Strongly retrieve every evaluator's V3 result for one admission; V2 is not read."""
        ...


@runtime_checkable
class AgentQualityAggregatePort(Protocol):
    """Write immutable aggregate-ready bounded measurements."""

    def write_metric(self, metric: AuditAggregateMetricV2) -> AuditWriteResult:
        """Write one immutable per-receipt aggregate measurement."""
        ...
