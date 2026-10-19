# SPDX-License-Identifier: MIT-0
"""CloudFormation on-event admission for declared preset-artifact backfills."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, cast
from uuid import uuid4

from aws_lambda_powertools import Logger

from adapters.config import preset_artifact_backfill_settings, tracing_settings
from adapters.inbound.tool_handlers.gateway import contracts_root
from adapters.jobs.execution_graph.cutover_gate import CutoverGate, provisioned_cutover_gate
from adapters.jobs.preset_artifact_publisher.transport import (
    BakeIntentSender,
    DeclaredBakeIntents,
    SqsBakeIntentSender,
    load_declared_bake_intents,
)
from adapters.outbound.telemetry.bootstrap import configure_tracing, flushes_traces
from adapters.outbound.telemetry.reporting import (
    LogSeverity,
    OperationLog,
    Report,
    flushes_metrics,
    report_error,
    report_operation,
)
from adapters.outbound.telemetry.tracing import (
    PRESET_ARTIFACT_BACKFILL,
    span,
    trace_boundary,
)
from domain.operations.execution_graph_dispatch import BACKFILL_AFTER_CUTOVER

__all__ = ["BackfillApplication", "BackfillOutcome", "handler", "physical_resource_id"]

_GENERATION = re.compile(r"^[a-f0-9]{64}$")

_TRACING_SETTINGS = tracing_settings()
_TRACING = configure_tracing(_TRACING_SETTINGS)
LOGGER = Logger(service="adlytics-preset-artifact-backfill")
_OPERATION = "preset_artifact.backfill"
_COMPONENT = "preset_artifact_publisher.backfill"

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
class BackfillOutcome:
    """The CloudFormation response plus the bounded progress the request produced."""

    physical_resource_id: str
    generation: str
    request_type: str
    sent: int
    total: int

    def response(self) -> dict[str, str]:
        """Project only the custom-resource contract CloudFormation reads."""
        return {"PhysicalResourceId": self.physical_resource_id}


@dataclass(frozen=True, slots=True)
class BackfillApplication:
    """Enqueue all declaration-owned page variants for create/update only."""

    declared: DeclaredBakeIntents
    sender: BakeIntentSender
    gate: CutoverGate = field(default_factory=CutoverGate)
    """S6 §2.9: in GRAPH the backfill sends nothing (D11-independent) and is counted."""

    def apply(self, event: object) -> dict[str, str]:
        """Return a generation-bound stable physical ID without queue sends on deletion."""
        return self.run(event).response()

    def run(self, event: object) -> BackfillOutcome:
        """Admit the request, send every declared intent on create/update, and count them."""
        request = _request(event)
        generation = _generation(request)
        physical_id = physical_resource_id(generation)
        request_type = cast("str", request.get("RequestType"))
        if request_type == "Delete":
            return BackfillOutcome(physical_id, generation, request_type, sent=0, total=0)
        if request_type not in {"Create", "Update"}:
            raise ValueError("preset artifact backfill request type is unsupported.")
        if request_type == "Update" and _previous_generation(request) == generation:
            # R5-1: an Update that does not change the declared input (for example a re-sent
            # ServiceToken) enqueues nothing. Only a changed declaration re-fires the backfill.
            return BackfillOutcome(physical_id, generation, "Unchanged", sent=0, total=0)
        if self.gate.closed(BACKFILL_AFTER_CUTOVER, len(self.declared.intents)):
            # A legacy send in GRAPH would be a second, ungranted bake writer (§2.9); the next
            # granted run's bake re-evaluates every registered page instead.
            return BackfillOutcome(physical_id, generation, "AfterCutover", sent=0, total=0)
        sent = 0
        for intent in self.declared.intents:
            self.sender.send(intent)
            sent += 1
        return BackfillOutcome(
            physical_id, generation, request_type, sent=sent, total=len(self.declared.intents)
        )


def physical_resource_id(generation: str) -> str:
    """Name the retained custom-resource identity solely from packaged declaration content."""
    if not _GENERATION.fullmatch(generation):
        raise ValueError("preset artifact backfill generation must be a SHA-256 digest.")
    return f"preset-artifact-backfill-{generation}"


def _request(event: object) -> dict[str, object]:
    if not isinstance(event, dict):
        raise ValueError("preset artifact backfill event must be an object.")
    request = cast("dict[str, object]", event)
    if not isinstance(request.get("RequestType"), str):
        raise ValueError("preset artifact backfill event has no request type.")
    return request


def _generation(request: dict[str, object]) -> str:
    properties = request.get("ResourceProperties")
    if not isinstance(properties, dict):
        raise ValueError("preset artifact backfill has no resource properties.")
    document = cast("dict[str, object]", properties)
    # CloudFormation injects ServiceToken into every custom resource's properties.
    if set(document) - {"ServiceToken"} != {"generation"}:
        raise ValueError("preset artifact backfill properties are not closed.")
    generation = document.get("generation")
    if not isinstance(generation, str) or not _GENERATION.fullmatch(generation):
        raise ValueError("preset artifact backfill generation must be a SHA-256 digest.")
    return generation


def _previous_generation(request: dict[str, object]) -> str | None:
    """Return the Update's prior declared-input generation, or None when it has none."""
    previous = request.get("OldResourceProperties")
    if not isinstance(previous, dict):
        return None
    value = cast("dict[str, object]", previous).get("generation")
    return value if isinstance(value, str) else None


def _declared_generation(event: object) -> str:
    """Best-effort plan digest for lineage before admission has validated the request."""
    if isinstance(event, dict):
        properties = cast("dict[str, object]", event).get("ResourceProperties")
        if isinstance(properties, dict):
            generation = cast("dict[str, object]", properties).get("generation")
            if isinstance(generation, str) and _GENERATION.fullmatch(generation):
                return f"sha256:{generation}"
    return "unbound"


@lru_cache(maxsize=1)
def application() -> BackfillApplication:
    """Compose the standalone provider from the declared FIFO target and page declaration."""
    return BackfillApplication(
        load_declared_bake_intents(contracts_root()),
        SqsBakeIntentSender(preset_artifact_backfill_settings().bake_queue_url),
        provisioned_cutover_gate(),
    )


@trace_boundary(PRESET_ARTIFACT_BACKFILL)
@flushes_traces
@flushes_metrics
@LOGGER.inject_lambda_context
def handler(event: dict[str, Any], _context: object) -> dict[str, str]:
    """Expose the CloudFormation custom-resource on-event contract."""
    execution_id = str(getattr(_context, "aws_request_id", "") or uuid4().hex)
    composed = application()
    total = len(composed.declared.intents)
    plan_digest = _declared_generation(event)
    with span(
        PRESET_ARTIFACT_BACKFILL,
        operation_id=_OPERATION,
        execution_id=execution_id,
        plan_digest=plan_digest,
        stage="admission",
        completed=0,
        total=total,
    ) as process_span:
        try:
            outcome = composed.run(event)
        except Exception as error:
            report_error(
                error,
                Report(operation=_OPERATION, correlation_id=execution_id, logger=LOGGER),
            )
            report_operation(
                OperationLog(
                    operation=_OPERATION,
                    component=_COMPONENT,
                    phase="terminal",
                    outcome="failed",
                    severity=LogSeverity.WARNING,
                    details={
                        "execution_id": execution_id,
                        "plan_digest": plan_digest,
                        "stage": "admission",
                        "completed": 0,
                        "total": total,
                    },
                ),
                LOGGER,
            )
            raise
        process_span.record(
            stage="sent",
            completed=outcome.sent,
            total=outcome.total,
            outcome=outcome.request_type.lower(),
        )
        report_operation(
            OperationLog(
                operation=_OPERATION,
                component=_COMPONENT,
                phase="terminal",
                outcome=outcome.request_type.lower(),
                severity=LogSeverity.INFO,
                details={
                    "execution_id": execution_id,
                    "plan_digest": f"sha256:{outcome.generation}",
                    "stage": "sent",
                    "completed": outcome.sent,
                    "total": outcome.total,
                },
            ),
            LOGGER,
        )
        return outcome.response()
