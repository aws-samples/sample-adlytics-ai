# SPDX-License-Identifier: MIT-0
"""Stream and scheduled delivery of retained ADR-075 Stage-1 status authority."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, cast

from aws_lambda_powertools import Logger

from adapters.config import stage1_lifecycle_producer_settings
from adapters.outbound.dynamodb.stage1_operational_status import (
    DynamoStage1OperationalStatus,
)
from adapters.outbound.sqs.operational_status import SqsStage1OperationalStatusQueue
from adapters.outbound.telemetry.reporting import (
    LogSeverity,
    OperationLog,
    Report,
    flushes_metrics,
    report_error,
    report_operation,
)
from domain.operations.stage1_operational_status import (
    BlockErrorClass,
    BlockStage1OutboxRequest,
    DeliverStage1AuthorityRequest,
    DependencyErrorClass,
    LifecycleEventAuthority,
    OutboxState,
    RecordStage1AuthorityObservationRequest,
    RecordStage1OutboxDeliveryFailureRequest,
    SettledUsageContributionAuthority,
    Stage1OutboxAuthoritySnapshot,
    Stage1Surface,
    deliver_stage1_authority,
)
from domain.operations.status import MEASUREMENT_REFS, OperationalStatusError
from ports.outbound.stage1_operational_status import (
    Stage1OperationalStatusQueuePort,
    Stage1OutboxDeliveryStatePort,
    Stage1OutboxKey,
    Stage1RetainedAuthority,
)

_SETTINGS = stage1_lifecycle_producer_settings()
_LOGGER = Logger(service="adlytics-stage1-lifecycle-producer")
_OPERATION = "stage1_operational_status.delivery"
_COMPONENT = "stage1_lifecycle_producer"


class DeliveryOutcome(StrEnum):
    """Closed internal delivery disposition used by both invocation modes."""

    DELIVERED = "delivered"
    TERMINAL_NOOP = "terminal_noop"
    BLOCKED = "blocked"
    RETRY = "retry"


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """One key's internal disposition without retained payload disclosure."""

    outcome: DeliveryOutcome

    @property
    def retry(self) -> bool:
        """Return whether the owning source must retry this key."""
        return self.outcome is DeliveryOutcome.RETRY


@dataclass(frozen=True, slots=True)
class Stage1AuthorityDelivery:
    """The single send-before-mark use case shared by stream and schedule."""

    state: Stage1OutboxDeliveryStatePort
    queue: Stage1OperationalStatusQueuePort
    max_consecutive_failed_delivery_attempts: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_consecutive_failed_delivery_attempts, bool)
            or self.max_consecutive_failed_delivery_attempts < 1
        ):
            raise ValueError("Stage-1 delivery failure threshold must be positive")

    def deliver(  # noqa: PLR0911 - explicit terminal state machine branches
        self, key: Stage1OutboxKey, now: datetime | None = None
    ) -> DeliveryResult:
        """Strongly reread, send, then conditionally mark one exact authority."""
        instant = (now or datetime.now(tz=UTC)).astimezone(UTC)
        try:
            retained = self.state.authority(key.partition_key, key.record_key)
        except OperationalStatusError as error:
            snapshot = self.state.snapshot(key.partition_key, key.record_key)
            if snapshot is None or snapshot.outbox_state is not OutboxState.PENDING:
                return DeliveryResult(DeliveryOutcome.TERMINAL_NOOP)
            block_class = _validation_block_class(error)
            self._block(snapshot, instant, block_class, None)
            return DeliveryResult(DeliveryOutcome.BLOCKED)
        if retained is None or retained.snapshot.outbox_state is not OutboxState.PENDING:
            return DeliveryResult(DeliveryOutcome.TERMINAL_NOOP)
        try:
            retained = self._observe_lifecycle(retained, instant)
            decision = _delivery_request(retained, instant)
        except OperationalStatusError as error:
            self._block(
                retained.snapshot,
                instant,
                _validation_block_class(error),
                _member_surface(retained),
            )
            return DeliveryResult(DeliveryOutcome.BLOCKED)
        try:
            self.queue.send(decision.send_request)
        except Exception as error:
            report_error(error, Report(operation=_OPERATION, correlation_id="", logger=_LOGGER))
            return self._dependency_failure(
                retained.snapshot,
                instant,
                DependencyErrorClass.QUEUE_SEND_FAILED,
                BlockErrorClass.QUEUE_SEND_FAILED,
                _member_surface(retained),
            )
        try:
            self.state.mark_delivered(decision.mark_delivered_request)
        except Exception as error:
            report_error(error, Report(operation=_OPERATION, correlation_id="", logger=_LOGGER))
            return self._dependency_failure(
                retained.snapshot,
                instant,
                DependencyErrorClass.DELIVERY_MARK_FAILED,
                BlockErrorClass.DELIVERY_MARK_FAILED,
                _member_surface(retained),
            )
        return DeliveryResult(DeliveryOutcome.DELIVERED)

    def _observe_lifecycle(
        self, retained: Stage1RetainedAuthority, candidate_instant: datetime
    ) -> Stage1RetainedAuthority:
        if not isinstance(retained.member, LifecycleEventAuthority):
            return retained
        self.state.record_observation(
            RecordStage1AuthorityObservationRequest(retained.snapshot, candidate_instant)
        )
        reread = self.state.authority(retained.snapshot.partition_key, retained.snapshot.record_key)
        if reread is None or reread.snapshot.outbox_state is not OutboxState.PENDING:
            raise OperationalStatusError("lifecycle authority changed after observation")
        persisted = reread.snapshot.authority_observed_at
        if (
            persisted is None
            or not isinstance(reread.member, LifecycleEventAuthority)
            or reread.member.event.authority.observed_at != persisted
        ):
            raise OperationalStatusError("lifecycle authority observation was not reconstructed")
        return reread

    def _dependency_failure(  # noqa: PLR0913, PLR0917 - closed internal transition
        self,
        authority: Stage1OutboxAuthoritySnapshot,
        instant: datetime,
        dependency_class: DependencyErrorClass,
        block_class: BlockErrorClass,
        surface: Stage1Surface | None,
    ) -> DeliveryResult:
        if (
            authority.consecutive_failed_delivery_attempts + 1
            < self.max_consecutive_failed_delivery_attempts
        ):
            self.state.record_failure(
                RecordStage1OutboxDeliveryFailureRequest(authority, instant, dependency_class)
            )
            return DeliveryResult(DeliveryOutcome.RETRY)
        self._block(authority, instant, block_class, surface)
        return DeliveryResult(DeliveryOutcome.BLOCKED)

    def _block(  # noqa: PLR0913, PLR0917 - closed internal transition
        self,
        authority: Stage1OutboxAuthoritySnapshot,
        instant: datetime,
        error_class: BlockErrorClass,
        surface: Stage1Surface | None,
    ) -> None:
        self.state.block(
            BlockStage1OutboxRequest(
                authority,
                instant,
                error_class,
                surface,
                str(uuid.uuid4()),
            )
        )


def _delivery_request(retained: Stage1RetainedAuthority, instant: datetime) -> Any:
    member = retained.member
    if isinstance(member, LifecycleEventAuthority):
        request = DeliverStage1AuthorityRequest(retained.snapshot, instant, lifecycle_event=member)
    elif isinstance(member, SettledUsageContributionAuthority):
        request = DeliverStage1AuthorityRequest(
            retained.snapshot, instant, usage_contribution=member
        )
    else:
        request = DeliverStage1AuthorityRequest(
            retained.snapshot, instant, usage_evidence_disposition=member
        )
    return deliver_stage1_authority(request)


def _member_surface(retained: Stage1RetainedAuthority) -> Stage1Surface | None:
    member = retained.member
    surface = (
        member.event.surface
        if isinstance(member, LifecycleEventAuthority)
        else member.contribution.evaluation_binding.measurement_refs
        if isinstance(member, SettledUsageContributionAuthority)
        else member.disposition.evaluation_binding.measurement_refs
    )
    if isinstance(surface, str):
        try:
            return Stage1Surface(surface)
        except ValueError:
            return None
    for candidate in Stage1Surface:
        if tuple(surface) == MEASUREMENT_REFS[candidate.value]:
            return candidate
    return None


def _validation_block_class(error: OperationalStatusError) -> BlockErrorClass:
    message = str(error).lower()
    if "digest" in message:
        return BlockErrorClass.IMMUTABLE_DIGEST_MISMATCH
    if "prefix/kind" in message or "prefix and kind" in message:
        return BlockErrorClass.PREFIX_KIND_MISMATCH
    if "delta" in message:
        return BlockErrorClass.FORBIDDEN_STAGE1_DELTA
    if "state" in message or "changed" in message:
        return BlockErrorClass.STATE_INVARIANT_VIOLATION
    return BlockErrorClass.SCHEMA_VALIDATION_FAILED


def _stream_key(  # noqa: PLR0911 - fail-closed event-envelope parsing
    value: object,
) -> Stage1OutboxKey | None:
    if not isinstance(value, dict):
        return None
    record = cast("dict[str, object]", value)
    if record.get("eventName") not in {"INSERT", "MODIFY"}:
        return None
    dynamodb = record.get("dynamodb")
    if not isinstance(dynamodb, dict):
        return None
    image = cast("dict[str, object]", dynamodb).get("NewImage")
    if not isinstance(image, dict):
        return None
    attributes = cast("dict[str, object]", image)
    values: list[str] = []
    for name in ("partition_key", "record_key"):
        raw = attributes.get(name)
        if not isinstance(raw, dict):
            return None
        text = cast("dict[str, object]", raw).get("S")
        if not isinstance(text, str) or not text:
            return None
        values.append(text)
    if not values[1].startswith(("EVENT#", "USAGE#")):
        return None
    return Stage1OutboxKey(*values)


def _sequence_number(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    dynamodb = cast("dict[str, object]", value).get("dynamodb")
    if not isinstance(dynamodb, dict):
        return None
    sequence = cast("dict[str, object]", dynamodb).get("SequenceNumber")
    return sequence if isinstance(sequence, str) and sequence else None


def apply_stream(event: object, delivery: Stage1AuthorityDelivery) -> tuple[str, ...]:
    """Use DynamoDB SequenceNumber for only below-threshold dependency retries."""
    if not isinstance(event, dict):
        raise OperationalStatusError("Stage-1 stream invocation must be an object")
    records = cast("dict[str, object]", event).get("Records")
    if not isinstance(records, list):
        raise OperationalStatusError("Stage-1 stream invocation requires Records")
    failures: list[str] = []
    for raw in cast("list[object]", records):
        key = _stream_key(raw)
        if key is None:
            continue
        try:
            result = delivery.deliver(key)
        except Exception as error:
            report_error(error, Report(operation=_OPERATION, correlation_id="", logger=_LOGGER))
            result = DeliveryResult(DeliveryOutcome.RETRY)
        if result.retry:
            sequence = _sequence_number(raw)
            if sequence is not None:
                failures.append(sequence)
    return tuple(failures)


def apply_schedule(delivery: Stage1AuthorityDelivery, limit: int) -> dict[str, int]:
    """Continue after terminal/block outcomes and stop only on transient dependency failure."""
    page = delivery.state.pending(limit)
    delivered = blocked = terminal = retried = 0
    for key in page.keys:
        result = delivery.deliver(key)
        if result.outcome is DeliveryOutcome.DELIVERED:
            delivered += 1
        elif result.outcome is DeliveryOutcome.BLOCKED:
            blocked += 1
        elif result.outcome is DeliveryOutcome.TERMINAL_NOOP:
            terminal += 1
        else:
            retried += 1
            break
    return {
        "queried": len(page.keys),
        "delivered": delivered,
        "blocked": blocked,
        "terminal_noop": terminal,
        "retry": retried,
    }


def _mode(event: object) -> str:
    if not isinstance(event, dict):
        raise OperationalStatusError("Stage-1 lifecycle invocation must be an object")
    document = cast("dict[str, object]", event)
    stream = "Records" in document
    scheduled = document.get("source") == "aws.events" and (
        document.get("detail-type") == "Scheduled Event"
    )
    if stream == scheduled:
        raise OperationalStatusError("Stage-1 lifecycle invocation mode is ambiguous or unknown")
    return "stream" if stream else "reconcile"


_DELIVERY = Stage1AuthorityDelivery(
    DynamoStage1OperationalStatus(_SETTINGS.publications_table, _SETTINGS.status_table),
    SqsStage1OperationalStatusQueue(_SETTINGS.queue_url),
    _SETTINGS.max_consecutive_failed_delivery_attempts,
)


@flushes_metrics
@_LOGGER.inject_lambda_context
def handler(event: dict[str, Any], _context: object) -> dict[str, object]:
    """Dispatch the exact stream or schedule shape through one delivery use case."""
    mode = _mode(event)
    if mode == "stream":
        failures = apply_stream(event, _DELIVERY)
        result: dict[str, object] = {
            "batchItemFailures": [{"itemIdentifier": identifier} for identifier in failures]
        }
        failed = len(failures)
        outcome = "completed" if failed == 0 else "partial_retry"
        details: dict[str, object] = {"failed_count": failed}
    else:
        scheduled = apply_schedule(_DELIVERY, _SETTINGS.reconcile_batch_size)
        result = dict(scheduled)
        outcome = "completed" if scheduled["retry"] == 0 else "partial_retry"
        details = result
    report_operation(
        OperationLog(
            operation=_OPERATION,
            component=_COMPONENT,
            phase="terminal",
            outcome=outcome,
            severity=LogSeverity.INFO if outcome == "completed" else LogSeverity.WARNING,
            details=details,
        ),
        _LOGGER,
    )
    return result
