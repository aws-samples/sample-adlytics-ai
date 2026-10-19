# SPDX-License-Identifier: MIT-0
"""Versioned generic OTel projection for exact released-response evaluation."""

from __future__ import annotations

import json
from dataclasses import dataclass

from domain.evaluation.envelope import EvaluationEnvelope

_TRACE_ID_LENGTH = 32
_SPAN_ID_LENGTH = 16


@dataclass(frozen=True, slots=True)
class ReleasedSpanProjection:
    """Inputs required to serialize one ReleasedEvaluationSpan.v3 document."""

    envelope: EvaluationEnvelope
    service_name: str
    timestamp_ns: int
    trace_id: str
    span_id: str

    def __post_init__(self) -> None:
        if not self.service_name.strip():
            raise ValueError("released evaluation span requires a service name")
        if self.timestamp_ns <= 1:
            raise ValueError("released evaluation span timestamp is invalid")
        if len(self.trace_id) != _TRACE_ID_LENGTH or len(self.span_id) != _SPAN_ID_LENGTH:
            raise ValueError("released evaluation span identifiers are invalid")


def released_span_json(projection: ReleasedSpanProjection) -> str:
    """Serialize the closed ReleasedEvaluationSpan.v3 source contract."""
    envelope = projection.envelope
    evaluation_input = envelope.question
    if envelope.grounded_context:
        evaluation_input = (
            f"User request:\n{envelope.question}\n\n"
            f"Trusted governed evidence and verification:\n{envelope.grounded_context}"
        )
    attributes: dict[str, str] = {
        "agent_quality.audit_id": envelope.audit_id,
        "agentcore.invocation.agent_response": envelope.released_answer,
        "agentcore.invocation.user_prompt": evaluation_input,
        "gen_ai.operation.name": "invoke_agent",
        "gen_ai.task.input": evaluation_input,
        "gen_ai.task.output": envelope.released_answer,
        "session.id": envelope.correlation_id,
    }
    attributes["agent_quality.evaluation_admission_ref"] = envelope.evaluation_admission_ref
    document = {
        "attributes": attributes,
        "durationNano": 1,
        "endTimeUnixNano": projection.timestamp_ns,
        "kind": "INTERNAL",
        "name": "invoke_agent adlytics released response",
        "resource": {
            "attributes": {
                "aws.service.type": "gen_ai_agent",
                "service.name": projection.service_name,
            }
        },
        "scope": {"name": "opentelemetry.instrumentation.adlytics_evaluation", "version": "3"},
        "spanId": projection.span_id,
        "startTimeUnixNano": projection.timestamp_ns - 1,
        "status": {"code": "OK"},
        "traceId": projection.trace_id,
    }
    return json.dumps(document, separators=(",", ":"), ensure_ascii=False)
