# SPDX-License-Identifier: MIT-0
# ruff: noqa: PLR0911
"""Retained-publication stream admission for declared preset-artifact bakes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import Any, Protocol, cast
from uuid import uuid4

from aws_lambda_powertools import Logger

from adapters.config import preset_artifact_enqueue_settings, tracing_settings
from adapters.inbound.tool_handlers.gateway import contracts_root
from adapters.jobs.execution_graph.cutover_gate import (
    CutoverGate,
    provisioned_cutover_gate,
    record_count,
)
from adapters.jobs.preset_artifact_publisher.transport import (
    BakeIntent,
    BakeIntentSender,
    DeclaredBakeIntents,
    SqsBakeIntentSender,
    load_declared_bake_intents,
)
from adapters.outbound.dynamodb.semantic_state import DynamoCurrentPublications
from adapters.outbound.operational_companions import companion_emitter
from adapters.outbound.telemetry.bootstrap import configure_tracing, flushes_traces
from adapters.outbound.telemetry.reporting import (
    METRICS,
    LogSeverity,
    OperationLog,
    Report,
    report_error,
    report_operation,
)
from adapters.outbound.telemetry.tracing import (
    PRESET_ARTIFACT_ENQUEUE,
    PRESET_ARTIFACT_ENQUEUE_DELIVERY,
    span,
    trace_boundary,
)
from domain.operations.companions import (
    CompanionFlowRequest,
    CompanionTerminal,
    companion_flow,
)
from domain.operations.execution_graph_dispatch import STREAM_TRIGGER_AFTER_CUTOVER
from domain.operations.status import CompletionOutcome, FlowState, Progress
from domain.semantics.publication import SuccessfulPublication

__all__ = [
    "EnqueueTelemetry",
    "PresetArtifactEnqueueApplication",
    "PublicationTrigger",
    "apply_records",
    "batch_telemetry",
    "handler",
    "parse_record",
]

_TRACING_SETTINGS = tracing_settings()
_TRACING = configure_tracing(_TRACING_SETTINGS)
LOGGER = Logger(service="adlytics-preset-artifact-enqueue")
_OPERATION = "preset_artifact.enqueue"
_COMPONENT = "preset_artifact_publisher.enqueue"

report_operation(
    OperationLog(
        operation=_OPERATION,
        component=_COMPONENT,
        phase="start",
        outcome="active" if _TRACING else "disabled",
        severity=LogSeverity.DEBUG,
        details={
            "stage": "startup",
            "tracing": _TRACING,
            "sample_ratio": _TRACING_SETTINGS.sample_ratio,
        },
    ),
    LOGGER,
)


@dataclass(frozen=True, slots=True)
class PublicationTrigger:
    """The sole identity admitted from a CURRENT stream record."""

    partition_key: str
    publication_id: str


class PublicationTriggerReader(Protocol):
    """Re-attest a CURRENT flip through retained immutable publication state."""

    def current_for_trigger(
        self, partition_key: str, publication_id: str
    ) -> SuccessfulPublication | None:
        """Return current retained publication or stable absence for a superseded trigger."""
        ...


class PublicationIntentPlanner(Protocol):
    """Map a retained publication to statically registered declared bake targets."""

    def intents_for(self, publication: SuccessfulPublication) -> tuple[BakeIntent, ...]:
        """Return only declaration-owned page/variant intents."""
        ...


@dataclass(frozen=True, slots=True)
class CanonicalPublicationIntentPlanner:
    """Route only retained canonical-curation publications to declared page variants."""

    declared: DeclaredBakeIntents

    def intents_for(self, publication: SuccessfulPublication) -> tuple[BakeIntent, ...]:
        """Ignore all event fields and trust the reader-attested publisher identity only."""
        return self.declared.intents if publication.publisher.value == "canonical-curation" else ()


@dataclass(frozen=True, slots=True)
class PresetArtifactEnqueueApplication:
    """Read-attest-plan-send application for one accepted DynamoDB stream delivery."""

    reader: PublicationTriggerReader
    planner: PublicationIntentPlanner
    sender: BakeIntentSender
    declared: DeclaredBakeIntents
    gate: CutoverGate = field(default_factory=CutoverGate)
    """S6 §2.9 (D6): in GRAPH every stream record is dropped with zero effect and counted."""

    def apply(self, trigger: PublicationTrigger) -> None:
        """Enqueue only current, retained publications and declared targets."""
        publication = self.reader.current_for_trigger(trigger.partition_key, trigger.publication_id)
        if publication is None:
            return
        intents = self.planner.intents_for(publication)
        if any(not self.declared.contains(intent) for intent in intents):
            raise ValueError("preset artifact planner returned an undeclared bake intent.")
        for intent in intents:
            self.sender.send(intent)
        _emit_enqueue_companion(trigger, publication, len(intents))


def _emit_enqueue_companion(
    trigger: PublicationTrigger, publication: SuccessfulPublication, admitted: int
) -> None:
    """Report the attested admission decision as a deterministic two-event companion flow.

    Every identity and instant derives from the re-attested publication, so a stream
    redelivery reproduces byte-identical events that deduplicate at the projection.
    """
    flow = companion_flow(
        CompanionFlowRequest(
            surface="preset_enqueue",
            authority_kind="current_publication",
            identity={
                "partition": trigger.partition_key,
                "publication_id": trigger.publication_id,
            },
            started_at=publication.published_at,
            start_state=FlowState.OBSERVED,
            start_stage="current_transition_observed",
        )
    )
    terminal = flow.terminal(
        CompanionTerminal(
            state=FlowState.COMPLETED,
            outcome=(
                CompletionOutcome.SUCCEEDED if admitted else CompletionOutcome.COMPLETED_NO_CHANGE
            ),
            stage="enqueue_decision_attested",
            occurred_at=publication.published_at,
            authority_digest_value={
                "partition": trigger.partition_key,
                "publication_id": trigger.publication_id,
                "admitted": str(admitted),
            },
            reason_code=None if admitted else "no_declared_targets",
            progress=Progress(admitted, max(admitted, 1), "intents") if admitted else None,
        )
    )
    companion_emitter(_OPERATION).emit(flow.start, terminal)


def _servable_transition(image: dict[str, object], publication_id: str) -> bool:
    """Apply the servable trigger rule owned by generation coordination §3.

    A record whose ``projected_publication_id`` equals the authority publication id is the
    applied (servable) transition. An absent attribute is a legacy authority advance from
    the pre-migration window and is processed. Anything else — an authority-only advance
    or a superseded servable move — is acknowledged without work.
    """
    projected = image.get("projected_publication_id")
    if projected is None:
        return True
    if not isinstance(projected, dict):
        return False
    return cast("dict[str, object]", projected).get("S") == publication_id


def parse_record(record: object) -> PublicationTrigger | None:
    """Accept only a DynamoDB INSERT/MODIFY NEW_IMAGE CURRENT publication record."""
    if not isinstance(record, dict):
        return None
    event = cast("dict[str, object]", record)
    if event.get("eventName") not in {"INSERT", "MODIFY"}:
        return None
    dynamodb = event.get("dynamodb")
    if not isinstance(dynamodb, dict):
        return None
    image = cast("dict[str, object]", dynamodb).get("NewImage")
    if not isinstance(image, dict):
        return None
    values = cast("dict[str, object]", image)
    partition, record_key, payload = (
        values.get("partition_key"),
        values.get("record_key"),
        values.get("payload"),
    )
    if not all(isinstance(value, dict) for value in (partition, record_key, payload)):
        return None
    partition_value = cast("dict[str, object]", partition).get("S")
    record_key_value = cast("dict[str, object]", record_key).get("S")
    payload_value = cast("dict[str, object]", payload).get("S")
    if (
        not isinstance(partition_value, str)
        or not partition_value
        or record_key_value != "CURRENT"
        or not isinstance(payload_value, str)
    ):
        return None
    try:
        decoded: object = json.loads(payload_value)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, dict):
        return None
    publication_id = cast("dict[str, object]", decoded).get("publication_id")
    if not isinstance(publication_id, str) or not publication_id:
        return None
    if not _servable_transition(values, publication_id):
        return None
    return PublicationTrigger(partition_value, publication_id)


@dataclass(frozen=True, slots=True)
class EnqueueTelemetry:
    """Lineage shared by every span and lifecycle record of one stream batch."""

    execution_id: str
    plan_digest: str
    total: int
    after_cutover: bool | None = None
    """S6 §2.9: the handler's one mode read for this batch; None when it has not read it."""


def batch_telemetry(event: object, execution_id: str) -> EnqueueTelemetry:
    """Digest only the admitted trigger identities, never the raw stream images."""
    records: list[object] = []
    if isinstance(event, dict):
        raw = cast("dict[str, object]", event).get("Records")
        if isinstance(raw, list):
            records = cast("list[object]", raw)
    triggers = [
        [trigger.partition_key, trigger.publication_id]
        for trigger in (parse_record(record) for record in records)
        if trigger is not None
    ]
    encoded = json.dumps(triggers, separators=(",", ":")).encode()
    return EnqueueTelemetry(
        execution_id=execution_id,
        plan_digest=f"sha256:{hashlib.sha256(encoded).hexdigest()}",
        total=len(triggers),
    )


def _sequence_number(record: object) -> str | None:
    if not isinstance(record, dict):
        return None
    raw_dynamodb = cast("dict[str, object]", record).get("dynamodb")
    if not isinstance(raw_dynamodb, dict):
        return None
    sequence_number = cast("dict[str, object]", raw_dynamodb).get("SequenceNumber")
    return sequence_number if isinstance(sequence_number, str) and sequence_number else None


@trace_boundary(PRESET_ARTIFACT_ENQUEUE_DELIVERY)
def apply_records(
    event: object,
    application: PresetArtifactEnqueueApplication,
    telemetry: EnqueueTelemetry | None = None,
) -> tuple[str, ...]:
    """Return only valid stream delivery IDs that need retry after a dependency failure."""
    if not isinstance(event, dict):
        return ()
    records = cast("dict[str, object]", event).get("Records")
    if not isinstance(records, list):
        return ()
    invocation = telemetry or batch_telemetry(cast("dict[str, object]", event), uuid4().hex)
    closed = invocation.after_cutover
    if closed is None:
        closed = application.gate.closed(
            STREAM_TRIGGER_AFTER_CUTOVER, len(cast("list[object]", records))
        )
    if closed:
        # D6: the mode was read once for the batch, before any record is parsed.
        return ()
    failures: list[str] = []
    completed = 0
    for record in cast("list[object]", records):
        trigger = parse_record(record)
        if trigger is None:
            continue
        with span(
            PRESET_ARTIFACT_ENQUEUE_DELIVERY,
            operation_id=_OPERATION,
            execution_id=invocation.execution_id,
            plan_digest=invocation.plan_digest,
            stage="delivery",
            completed=completed,
            total=invocation.total,
        ) as delivery_span:
            try:
                application.apply(trigger)
                completed += 1
                delivery_span.record(completed=completed)
            except Exception as error:
                report_error(
                    error,
                    Report(
                        operation=_OPERATION,
                        correlation_id=invocation.execution_id,
                        logger=LOGGER,
                    ),
                )
                sequence_number = _sequence_number(record)
                if sequence_number is not None:
                    failures.append(sequence_number)
    return tuple(failures)


@lru_cache(maxsize=1)
def application() -> PresetArtifactEnqueueApplication:
    """Compose the stream worker from the typed worker-only settings accessor."""
    settings = preset_artifact_enqueue_settings()
    declared = load_declared_bake_intents(contracts_root())
    return PresetArtifactEnqueueApplication(
        reader=DynamoCurrentPublications(settings.semantic_publications_table),
        planner=CanonicalPublicationIntentPlanner(declared),
        sender=SqsBakeIntentSender(settings.bake_queue_url),
        declared=declared,
        gate=provisioned_cutover_gate(),
    )


@trace_boundary(PRESET_ARTIFACT_ENQUEUE)
@flushes_traces
@METRICS.log_metrics
@LOGGER.inject_lambda_context
def handler(event: dict[str, Any], _context: object) -> dict[str, list[dict[str, str]]]:
    """Use DynamoDB partial-batch retry for only failed retained publication deliveries."""
    execution_id = str(getattr(_context, "aws_request_id", "") or uuid4().hex)
    # S6 §2.9 (D6): one mode read per batch, before any record is parsed (even for telemetry).
    closed = application().gate.closed(STREAM_TRIGGER_AFTER_CUTOVER, record_count(event))
    telemetry = replace(
        batch_telemetry({} if closed else event, execution_id), after_cutover=closed
    )
    with span(
        PRESET_ARTIFACT_ENQUEUE,
        operation_id=_OPERATION,
        execution_id=telemetry.execution_id,
        plan_digest=telemetry.plan_digest,
        stage="batch",
        completed=0,
        total=telemetry.total,
    ) as process_span:
        failures = apply_records(event, application(), telemetry)
        completed = telemetry.total - len(failures)
        outcome = "completed" if not failures else "retry"
        process_span.record(completed=completed, outcome=outcome)
        report_operation(
            OperationLog(
                operation=_OPERATION,
                component=_COMPONENT,
                phase="terminal",
                outcome=outcome,
                severity=LogSeverity.INFO if not failures else LogSeverity.WARNING,
                details={
                    "execution_id": telemetry.execution_id,
                    "plan_digest": telemetry.plan_digest,
                    "stage": "batch",
                    "completed": completed,
                    "total": telemetry.total,
                },
            ),
            LOGGER,
        )
    return {"batchItemFailures": [{"itemIdentifier": item} for item in failures]}
