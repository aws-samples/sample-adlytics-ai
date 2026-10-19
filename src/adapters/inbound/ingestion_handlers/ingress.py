# SPDX-License-Identifier: MIT-0
"""The SQS S3-event handler as a `delivery_event` requester (S6, V-19; "Ingress by mode").

One strongly consistent mode read per message decides the path:

| Mode | Path |
|---|---|
| `LEGACY` | the HEAD legacy path, no admission call |
| `SHADOW` | the legacy path stays authoritative; after its outcome is durable, one epoch-0
  `delivery_event` admission is recorded. A recording failure is reported once as
  `SHADOW_RECORDING_FAILED` and never changes the legacy outcome or the acknowledgement |
| `LEGACY_DRAINING` | no legacy path and no admission call: the message stays unacknowledged
  and its visibility is set to the drain-wait expiry, `D` plus the queue margin. No receipt |
| `GRAPH` | admission; the receipt's `INSTANTIATE#` obligation completes the graph |

A `source_delivery` submission refused `AUTHORITY_MODE_DRAINING` (the drain CAS committed after
this message read `LEGACY` or `SHADOW`) takes the drain-wait path too, without spending a
receive on the default visibility (V-19(d)). That path reads the mode once more for `D`; if the
drain has already ended, the message is left for its default visibility and re-routed on its
next receive (`drain_ended`), never raised as an error. A refusal on the message's last allowed
receive is reported once as `DRAIN_LAST_RECEIVE` (its own alarmed operation); the message still
reaches the DLQ and is re-requested once after the decision.

The handler is a workload requester: it submits only `delivery_event` for its own deliveries,
so admission accepts its IAM role as a workload caller, never as an agent (S1A-7).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

from aws_lambda_powertools import Logger

from adapters.inbound.run_requests.service import RunRequest
from adapters.outbound.telemetry.reporting import (
    LogSeverity,
    OperationLog,
    Report,
    report_error,
    report_operation,
)
from domain.operations.cutover import (
    DRAIN_LAST_RECEIVE,
    drain_wait_visibility_seconds,
    is_last_receive,
)
from domain.operations.execution_graph import AuthorityMode, ModeState
from domain.operations.generated_execution_graphs import PROTOCOL_VERSION
from domain.shared_kernel.errors import AdlyticsError, DependencyError, ErrorCode, InvariantError
from ports.outbound.execution_graph import ReceiptRecord

__all__ = [
    "DRAIN_LAST_RECEIVE_OPERATION",
    "SHADOW_RECORDING_FAILED",
    "SHADOW_RECORDING_OPERATION",
    "DeliveryIngress",
    "DeliveryRef",
    "IngressPath",
    "delivery_request",
    "drain_ended",
    "drain_wait",
    "graph_admit",
    "ingress_path",
    "queue_url_from_arn",
    "record_schedule",
    "schedule_request",
    "shadow_record",
]

SHADOW_RECORDING_FAILED: Final = "SHADOW_RECORDING_FAILED"
SHADOW_RECORDING_OPERATION: Final = "ingest.shadow_recording"
DRAIN_LAST_RECEIVE_OPERATION: Final = "ingest.drain_last_receive"
_DELIVERY_EVENT: Final = "delivery_event"
_DELIVERY_REASON: Final = "delivery_arrived"


class IngressPath(StrEnum):
    """Which path one message takes, from one strong mode read."""

    LEGACY = "legacy"
    LEGACY_THEN_SHADOW = "legacy_then_shadow"
    DRAIN_WAIT = "drain_wait"
    GRAPH = "graph"


def ingress_path(mode: AuthorityMode) -> IngressPath:
    """Map the mode to the handler's path (the ingress-by-mode table)."""
    return {
        AuthorityMode.LEGACY: IngressPath.LEGACY,
        AuthorityMode.SHADOW: IngressPath.LEGACY_THEN_SHADOW,
        AuthorityMode.LEGACY_DRAINING: IngressPath.DRAIN_WAIT,
        AuthorityMode.GRAPH: IngressPath.GRAPH,
    }[mode]


class ShadowRecordingFailedError(DependencyError):
    """A SHADOW recording failed; reported once, and the legacy outcome stands."""

    def __init__(self, cause: str) -> None:
        super().__init__(
            ErrorCode("STAGE1_DELIVERY", 20),
            "the shadow delivery_event request was not recorded.",
            context={"signal": SHADOW_RECORDING_FAILED, "cause": cause},
        )


class DrainLastReceiveError(InvariantError):
    """A delivery refused during the drain on its last allowed receive; it reaches the DLQ."""

    def __init__(self, receive_count: int) -> None:
        super().__init__(
            ErrorCode("STAGE1_DELIVERY", 21),
            "a delivery was refused during the drain on its last allowed receive.",
            context={"signal": DRAIN_LAST_RECEIVE, "receive_count": receive_count},
        )


@dataclass(frozen=True, slots=True)
class DeliveryRef:
    """One SQS message's exact manifest version and its receive."""

    manifest_key: str
    version_id: str
    receipt_handle: str
    receive_count: int
    message_id: str
    queue_url: str


@dataclass(frozen=True, slots=True)
class DeliveryIngress:
    """The handler's requester dependencies; `admit` is None where no graph is provisioned."""

    mode: Callable[[], ModeState]
    admit: Callable[[RunRequest], ReceiptRecord] | None
    visibility: Callable[[str, str, int], None]
    """Set one message's visibility: (queue URL, receipt handle, seconds)."""
    requester: Callable[[], str]
    """The handler's own role ARN (one STS read per container)."""
    logger: Logger
    now: Callable[[], datetime] = lambda: datetime.now(UTC)


def queue_url_from_arn(arn: str) -> str:
    """Derive the queue URL from an SQS event source ARN (`arn:aws:sqs:<region>:<acct>:<name>`)."""
    parts = arn.split(":")
    if len(parts) != 6 or parts[:3] != ["arn", "aws", "sqs"]:  # noqa: PLR2004
        raise ValueError("an SQS record carries no valid eventSourceARN.")
    _, _, _, region, account, name = parts
    return f"https://sqs.{region}.amazonaws.com/{account}/{name}"


def delivery_request(ref: DeliveryRef, requester: str) -> RunRequest:
    """Build the deterministic `delivery_event` request for one exact manifest version.

    The request ID derives from the exact (key, version), so every redelivery of the same
    version is the same request (row 1 returns its receipt).
    """
    digest = hashlib.sha256(f"{ref.manifest_key}\n{ref.version_id}".encode()).hexdigest()
    return RunRequest(
        request_id=f"delivery-{digest[:48]}",
        protocol_version=PROTOCOL_VERSION,
        caller_kind=_DELIVERY_EVENT,
        reason=_DELIVERY_REASON,
        manifest_key=ref.manifest_key,
        version_id=ref.version_id,
        requester=requester,
        requester_is_iam=True,
        workload=True,
    )


def drain_wait(ingress: DeliveryIngress, ref: DeliveryRef, mode: ModeState) -> int:
    """Leave the message unacknowledged until `D` plus the margin; report a last receive once.

    `mode` is the read that routed the message here, never a second read: an abort that commits
    after the routing read must not turn this message's drain wait into an error (S6-10).
    """
    if mode.mode is not AuthorityMode.LEGACY_DRAINING or mode.drain_deadline is None:
        raise InvariantError(
            ErrorCode("STAGE1_DELIVERY", 22),
            "the drain-wait path needs a LEGACY_DRAINING mode with its deadline.",
            context={"mode": mode.mode.value},
        )
    seconds = drain_wait_visibility_seconds(
        datetime.fromisoformat(mode.drain_deadline), ingress.now()
    )
    ingress.visibility(ref.queue_url, ref.receipt_handle, seconds)
    if is_last_receive(ref.receive_count):
        report_error(
            DrainLastReceiveError(ref.receive_count),
            Report(
                operation=DRAIN_LAST_RECEIVE_OPERATION,
                correlation_id=ref.message_id,
                logger=ingress.logger,
            ),
        )
    report_operation(
        OperationLog(
            operation="ingest_delivery",
            component="ingest_handler",
            phase="drain_wait",
            outcome="left_unacknowledged",
            severity=LogSeverity.INFO,
            details={
                "manifest_key": ref.manifest_key,
                "receive_count": ref.receive_count,
                "visibility_seconds": seconds,
                "drain_deadline": mode.drain_deadline,
                "admission_calls": 0,
            },
        ),
        ingress.logger,
    )
    return seconds


def drain_ended(ingress: DeliveryIngress, ref: DeliveryRef, mode: ModeState) -> None:
    """Record that a V-19(d) refusal found the drain already ended; the message is re-routed."""
    report_operation(
        OperationLog(
            operation="ingest_delivery",
            component="ingest_handler",
            phase="drain_wait",
            outcome="drain_ended",
            severity=LogSeverity.INFO,
            details={
                "manifest_key": ref.manifest_key,
                "receive_count": ref.receive_count,
                "mode": mode.mode.value,
                "admission_calls": 0,
            },
        ),
        ingress.logger,
    )


def shadow_record(ingress: DeliveryIngress, ref: DeliveryRef) -> ReceiptRecord | None:
    """Record one epoch-0 `delivery_event` request after the legacy outcome is durable.

    Never raises: any failure is reported once as `SHADOW_RECORDING_FAILED`.
    """
    if ingress.admit is None:
        return None
    try:
        return ingress.admit(delivery_request(ref, ingress.requester()))
    except Exception as error:
        cause = str(error.code) if isinstance(error, AdlyticsError) else type(error).__name__
        report_error(
            ShadowRecordingFailedError(cause),
            Report(
                operation=SHADOW_RECORDING_OPERATION,
                correlation_id=ref.message_id,
                logger=ingress.logger,
            ),
        )
        return None


def schedule_request(manifest_key: str, version_id: str, requester: str) -> RunRequest:
    """Build the registration finalizer's deterministic `schedule` request (V-19)."""
    digest = hashlib.sha256(f"{manifest_key}\n{version_id}".encode()).hexdigest()
    return RunRequest(
        request_id=f"schedule-{digest[:48]}",
        protocol_version=PROTOCOL_VERSION,
        caller_kind="schedule",
        reason="scheduled_cadence",
        manifest_key=manifest_key,
        version_id=version_id,
        requester=requester,
        requester_is_iam=True,
        workload=True,
    )


def record_schedule(
    ingress: DeliveryIngress, manifest: tuple[str, str], correlation_id: str
) -> ReceiptRecord | None:
    """Make the finalizer's request for its stored manifest (key, version), by mode (V-19).

    `SHADOW`: one non-blocking epoch-0 request; a failure is reported once as
    `SHADOW_RECORDING_FAILED`. `GRAPH`: one request whose failure raises, so the registration
    is retried (the finalized status replays and the request ID repeats). `LEGACY` and
    `LEGACY_DRAINING`: no admission call; the manifest's S3 event waits on the delivery queue.
    """
    if ingress.admit is None:
        return None
    manifest_key, version_id = manifest
    mode = ingress.mode().mode
    if mode is AuthorityMode.GRAPH:
        return ingress.admit(schedule_request(manifest_key, version_id, ingress.requester()))
    if mode is not AuthorityMode.SHADOW:
        return None
    try:
        return ingress.admit(schedule_request(manifest_key, version_id, ingress.requester()))
    except Exception as error:
        cause = str(error.code) if isinstance(error, AdlyticsError) else type(error).__name__
        report_error(
            ShadowRecordingFailedError(cause),
            Report(
                operation=SHADOW_RECORDING_OPERATION,
                correlation_id=correlation_id,
                logger=ingress.logger,
            ),
        )
        return None


def graph_admit(ingress: DeliveryIngress, ref: DeliveryRef) -> ReceiptRecord:
    """Admit the delivery in `GRAPH`; the receipt is durable before the message is acknowledged."""
    if ingress.admit is None:
        raise InvariantError(
            ErrorCode("STAGE1_DELIVERY", 23), "GRAPH admission needs the graph authority."
        )
    receipt = ingress.admit(delivery_request(ref, ingress.requester()))
    report_operation(
        OperationLog(
            operation="ingest_delivery",
            component="ingest_handler",
            phase="admission",
            outcome=receipt.decision.lower(),
            severity=LogSeverity.INFO,
            details={
                "manifest_key": ref.manifest_key,
                "request_id": receipt.request_id,
                "decision": receipt.decision,
                "row": receipt.row or 0,
                "authority_epoch": receipt.authority_epoch,
                "run_id": receipt.run_id or "none",
            },
        ),
        ingress.logger,
    )
    return receipt
