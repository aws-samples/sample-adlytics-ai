# SPDX-License-Identifier: MIT-0
"""Managed evaluation writer contract and custody-derived identifiers."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from adapters.jobs.evaluation_writer.contract import decode_envelope, released_span_identifiers
from adapters.jobs.evaluation_writer.span import ReleasedSpanProjection, released_span_json

pytestmark = pytest.mark.unit

_CORRELATION = "123e4567-e89b-42d3-a456-426614174000"


def _document(**changes: object) -> dict[str, object]:
    document: dict[str, object] = {
        "audit_id": "audit-01",
        "correlation_id": _CORRELATION,
        "evaluation_admission_ref": "evaluation-admission-01",
        "grounded_context": '{"schema_version":"GroundedEvaluationContext.v1"}',
        "question": "What is the fictional governed result?",
        "released_answer": "The fictional released answer.",
        "schema_version": 3,
    }
    document.update(changes)
    return document


def test_v3_writer_envelope_requires_exact_grounded_schema_and_admission_reference() -> None:
    decoded = decode_envelope(json.dumps(_document()))

    assert decoded.evaluation_admission_ref == "evaluation-admission-01"
    span = json.loads(
        released_span_json(
            ReleasedSpanProjection(
                envelope=decoded,
                service_name="adlytics-evaluation",
                timestamp_ns=2,
                trace_id="a" * 32,
                span_id="b" * 16,
            )
        )
    )
    assert "prompt_use.id" not in span["attributes"]
    assert (
        "Trusted governed evidence and verification:"
        in span["attributes"]["agentcore.invocation.user_prompt"]
    )

    legacy = _document(schema_version=2)
    legacy.pop("grounded_context")
    assert decode_envelope(json.dumps(legacy)).grounded_context == ""

    for invalid in (
        _document(prompt_use_id="A" * 43),
        _document(schema_version=1),
        _document(evaluation_admission_ref=None),
        _document(evaluation_admission_ref=1),
        _document(question=1),
        _document(grounded_context="x" * (240 * 1024)),
        _document(unexpected="field"),
    ):
        with pytest.raises(ValueError):
            decode_envelope(json.dumps(invalid))


def test_released_span_identifiers_are_determined_only_by_admission_reference() -> None:
    first = decode_envelope(json.dumps(_document(audit_id="audit-01")))
    same_admission = decode_envelope(json.dumps(_document(audit_id="audit-02")))
    other_admission = decode_envelope(
        json.dumps(_document(evaluation_admission_ref="evaluation-admission-02"))
    )

    assert released_span_identifiers(first) == released_span_identifiers(same_admission)
    assert released_span_identifiers(first) != released_span_identifiers(other_admission)


def test_released_span_keeps_released_answer_exact_and_unmodified_without_grounding() -> None:
    answer = "Released answer with exact spacing.\nSecond line."
    decoded = decode_envelope(json.dumps(_document(released_answer=answer, grounded_context="")))

    span = json.loads(
        released_span_json(
            ReleasedSpanProjection(
                envelope=decoded,
                service_name="adlytics-evaluation",
                timestamp_ns=2,
                trace_id="a" * 32,
                span_id="b" * 16,
            )
        )
    )
    attributes = span["attributes"]

    assert attributes["agentcore.invocation.agent_response"] == answer
    assert attributes["gen_ai.task.output"] == answer
    assert attributes["agentcore.invocation.user_prompt"] == decoded.question
    assert attributes["gen_ai.task.input"] == decoded.question


@dataclass(frozen=True)
class _InvalidProjection:
    service_name: str
    timestamp_ns: int
    trace_id: str
    span_id: str


@pytest.mark.parametrize(
    "case",
    [
        _InvalidProjection("", 2, "a" * 32, "b" * 16),
        _InvalidProjection("adlytics-evaluation", 1, "a" * 32, "b" * 16),
        _InvalidProjection("adlytics-evaluation", 2, "a" * 31, "b" * 16),
        _InvalidProjection("adlytics-evaluation", 2, "a" * 32, "b" * 15),
    ],
)
def test_released_span_projection_rejects_invalid_identity_or_timestamp(
    case: _InvalidProjection,
) -> None:
    envelope = decode_envelope(json.dumps(_document()))

    with pytest.raises(ValueError):
        ReleasedSpanProjection(
            envelope=envelope,
            service_name=case.service_name,
            timestamp_ns=case.timestamp_ns,
            trace_id=case.trace_id,
            span_id=case.span_id,
        )


def test_released_span_has_exact_timing_status_and_resource_contract() -> None:
    envelope = decode_envelope(json.dumps(_document()))
    projection = ReleasedSpanProjection(
        envelope=envelope,
        service_name="adlytics-evaluation",
        timestamp_ns=42,
        trace_id="a" * 32,
        span_id="b" * 16,
    )

    span = json.loads(released_span_json(projection))

    assert span["durationNano"] == 1
    assert span["startTimeUnixNano"] == 41
    assert span["endTimeUnixNano"] == 42
    assert span["status"] == {"code": "OK"}
    assert span["resource"] == {
        "attributes": {
            "aws.service.type": "gen_ai_agent",
            "service.name": "adlytics-evaluation",
        }
    }
    assert span["scope"] == {
        "name": "opentelemetry.instrumentation.adlytics_evaluation",
        "version": "3",
    }
