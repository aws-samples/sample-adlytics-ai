# SPDX-License-Identifier: MIT-0
"""Bounded in-memory projection of protected CloudWatch subscription events."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from domain.evaluation.linkage import (
    EvaluationDeliveryFamilyV1,
    EvaluationResultStateV1,
    EvaluationResultV3,
    normalize_scale_label,
)
from domain.evaluation.release import EvaluationReleaseEvaluatorV1

_MAX_ENCODED_BYTES = 1_400_000
_MAX_COMPRESSED_BYTES = 1_000_000
_MAX_DECOMPRESSED_BYTES = 4_000_000
_MAX_LOG_EVENTS = 100
_MAX_MESSAGE_BYTES = 250_000
_TRACE_ID_LENGTH = 32
_ALLOWED_SCORES = frozenset({0.0, 0.5, 0.75, 1.0})
_SCALE_LABEL = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_OPAQUE_REFERENCE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_EVALUATOR_ARN_MARKER = ":evaluator/"
_RESULT_FIELDS = frozenset(
    {
        "traceId",
        "session.id",
        "onlineEvaluationConfigId",
        "aws.bedrock_agentcore.evaluator.arn",
        "gen_ai.evaluation.name",
        "gen_ai.evaluation.score.label",
        "gen_ai.evaluation.score.value",
    }
)
_MAX_FIELD_NODES = 256
_MAX_FIELD_DEPTH = 8


@dataclass(frozen=True, slots=True)
class EvaluationResultCandidate:
    """Transient protected-result fields required for two-reference attestation only.

    `config_id` and `evaluator_ref` are the record's own claims; the ingress admits the result
    only when the turn's delivery receipt says the row was sent to exactly that pair (ADR-079).
    """

    correlation_id: str
    trace_id: str
    config_id: str
    evaluator_ref: str
    evaluator_name: str
    result_ref: str
    state: EvaluationResultStateV1
    label: str | None
    score: float | None
    evaluated_at: str

    def result(self, request: EvaluationResultStampRequest) -> EvaluationResultV3:
        """Construct the durable result, stamping its provenance from the delivery receipt."""
        evaluator = request.evaluator
        return EvaluationResultV3(
            audit_id=request.audit_id,
            admission_ref=request.admission_ref,
            evaluator_ref=self.evaluator_ref,
            evaluator_name=evaluator.evaluator_name,
            result_ref=self.result_ref,
            state=self.state,
            label=self.label,
            score=self.score,
            evaluated_at=self.evaluated_at,
            family=request.family.family,
            config_id=request.family.config_id,
            evaluator_revision=evaluator.revision,
            model_id=evaluator.model_id,
            prompt_digest=evaluator.prompt_digest,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class EvaluationResultStampRequest:
    """The attested target and the receipt entry a candidate's provenance is stamped from."""

    audit_id: str
    admission_ref: str
    family: EvaluationDeliveryFamilyV1
    evaluator: EvaluationReleaseEvaluatorV1

    def __post_init__(self) -> None:
        if self.family.evaluator(self.evaluator.evaluator_id) != self.evaluator:
            raise ValueError("a stamped evaluator must belong to its delivered family.")


@dataclass(frozen=True, slots=True)
class _SubscriptionContext:
    log_group: str
    log_stream: str


@dataclass(frozen=True, slots=True)
class _LogEvent:
    event_id: str
    timestamp: int
    message: str


def candidates_from_subscription(
    event: object, *, expected_log_groups: frozenset[str]
) -> tuple[EvaluationResultCandidate, ...]:
    """Decode one subscription batch without returning or logging protected source content.

    A batch is accepted only from one of the owned result groups. Each record's configuration
    and evaluator identity are read from the record itself; whether the turn was sent to that
    pair is decided afterwards by its delivery receipt, never by deployment configuration.
    """
    payload = _payload(event)
    if payload.get("messageType") == "CONTROL_MESSAGE":
        return ()
    log_group = payload.get("logGroup")
    if (
        payload.get("messageType") != "DATA_MESSAGE"
        or not isinstance(log_group, str)
        or log_group not in expected_log_groups
    ):
        raise ValueError("evaluation result subscription source is invalid")
    log_stream = payload.get("logStream")
    log_events_value = payload.get("logEvents")
    if not isinstance(log_stream, str) or not log_stream or not isinstance(log_events_value, list):
        raise ValueError("evaluation result subscription event is malformed")
    log_events = cast("list[object]", log_events_value)
    if len(log_events) > _MAX_LOG_EVENTS:
        raise ValueError("evaluation result subscription batch is too large")
    context = _SubscriptionContext(log_group=log_group, log_stream=log_stream)
    candidates: list[EvaluationResultCandidate] = []
    for value in log_events:
        candidate = _candidate(value, context)
        if candidate is not None:
            candidates.append(candidate)
    return tuple(candidates)


def _payload(event: object) -> dict[str, object]:
    if not isinstance(event, dict):
        raise ValueError("evaluation result subscription event is malformed")
    awslogs = cast("dict[str, object]", event).get("awslogs")
    if not isinstance(awslogs, dict):
        raise ValueError("evaluation result subscription envelope is malformed")
    encoded = cast("dict[str, object]", awslogs).get("data")
    if not isinstance(encoded, str) or not encoded or len(encoded.encode()) > _MAX_ENCODED_BYTES:
        raise ValueError("evaluation result subscription data is invalid")
    try:
        compressed = base64.b64decode(encoded, validate=True)
    except ValueError:
        raise ValueError("evaluation result subscription data is invalid") from None
    if len(compressed) > _MAX_COMPRESSED_BYTES:
        raise ValueError("evaluation result subscription data is too large")
    decompressor = zlib.decompressobj(zlib.MAX_WBITS | 16)
    try:
        decoded = decompressor.decompress(compressed, _MAX_DECOMPRESSED_BYTES + 1)
        remaining = _MAX_DECOMPRESSED_BYTES + 1 - len(decoded)
        if remaining > 0:
            decoded += decompressor.flush(remaining)
    except zlib.error:
        raise ValueError("evaluation result subscription data is invalid") from None
    if (
        len(decoded) > _MAX_DECOMPRESSED_BYTES
        or decompressor.unused_data
        or decompressor.unconsumed_tail
    ):
        raise ValueError("evaluation result subscription data is too large")
    try:
        value: object = json.loads(decoded)
    except UnicodeDecodeError, json.JSONDecodeError:
        raise ValueError("evaluation result subscription data is invalid") from None
    if not isinstance(value, dict):
        raise ValueError("evaluation result subscription payload is malformed")
    return cast("dict[str, object]", value)


def _candidate(value: object, context: _SubscriptionContext) -> EvaluationResultCandidate | None:
    event = _log_event(value)
    if event is None:
        return None
    fields = _message_fields(event.message)
    if fields is None:
        return None
    config_id = fields.get("onlineEvaluationConfigId")
    if not isinstance(config_id, str) or not _OPAQUE_REFERENCE.fullmatch(config_id):
        return None
    evaluator = _evaluator_identity(fields)
    trace_id = fields.get("traceId")
    correlation_id = fields.get("session.id")
    if evaluator is None or not _valid_references(trace_id, correlation_id):
        return None
    evaluator_ref, evaluator_name = evaluator
    label, score = _quality(fields)
    completed = label is not None and (score is None or score in _ALLOWED_SCORES)
    return EvaluationResultCandidate(
        correlation_id=cast("str", correlation_id),
        trace_id=cast("str", trace_id),
        config_id=config_id,
        evaluator_ref=evaluator_ref,
        evaluator_name=evaluator_name,
        result_ref=_result_reference(context, event.event_id),
        state=(
            EvaluationResultStateV1.COMPLETED
            if completed
            else EvaluationResultStateV1.EVALUATOR_FAILED
        ),
        label=label if completed else None,
        score=score if completed else None,
        evaluated_at=datetime.fromtimestamp(event.timestamp / 1_000, UTC).isoformat(),
    )


def _log_event(value: object) -> _LogEvent | None:
    if not isinstance(value, dict):
        return None
    raw = cast("dict[str, object]", value)
    event_id = raw.get("id")
    timestamp = raw.get("timestamp")
    message = raw.get("message")
    if (
        not isinstance(event_id, str)
        or not event_id
        or not isinstance(timestamp, int)
        or isinstance(timestamp, bool)
        or timestamp < 1
        or not isinstance(message, str)
        or len(message.encode()) > _MAX_MESSAGE_BYTES
    ):
        return None
    return _LogEvent(event_id=event_id, timestamp=timestamp, message=message)


@dataclass(slots=True)
class _FieldCollector:
    fields: dict[str, object]
    nodes: int = 0

    def collect(self, value: object, *, depth: int) -> bool:
        if depth > _MAX_FIELD_DEPTH:
            return False
        self.nodes += 1
        if self.nodes > _MAX_FIELD_NODES:
            return False
        if isinstance(value, dict):
            for key, item in cast("dict[str, object]", value).items():
                if key in _RESULT_FIELDS:
                    if key in self.fields:
                        return False
                    self.fields[key] = item
                elif not self.collect(item, depth=depth + 1):
                    return False
        elif isinstance(value, list):
            for item in cast("list[object]", value):
                if not self.collect(item, depth=depth + 1):
                    return False
        return True


def _message_fields(message: str) -> dict[str, object] | None:
    try:
        raw: object = json.loads(message)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    collector = _FieldCollector(fields={})
    root = cast("dict[str, object]", raw)
    return collector.fields if collector.collect(root, depth=0) else None


def _valid_references(trace_id: object, correlation_id: object) -> bool:
    return (
        isinstance(trace_id, str)
        and len(trace_id) == _TRACE_ID_LENGTH
        and all(character in "0123456789abcdef" for character in trace_id)
        and isinstance(correlation_id, str)
    )


def _evaluator_identity(fields: dict[str, object]) -> tuple[str, str] | None:
    """Read the evaluator's service ID from its ARN and its declared name from the record."""
    arn = fields.get("aws.bedrock_agentcore.evaluator.arn")
    name = fields.get("gen_ai.evaluation.name")
    if not isinstance(arn, str) or not isinstance(name, str):
        return None
    _, marker, evaluator_ref = arn.rpartition(_EVALUATOR_ARN_MARKER)
    if not marker or not _OPAQUE_REFERENCE.fullmatch(evaluator_ref):
        return None
    if not _OPAQUE_REFERENCE.fullmatch(name):
        return None
    return evaluator_ref, name


def _quality(fields: dict[str, object]) -> tuple[str | None, float | None]:
    """Read a verdict's label and, for a numerical scale, its score.

    A categorical scale emits a label and no score, so an absent score is not a failure; a score
    that is present but not a number is, and is reported as no label (ADR-082).
    """
    label_value = fields.get("gen_ai.evaluation.score.label")
    label = None
    if isinstance(label_value, str):
        normalized = normalize_scale_label(label_value)
        label = normalized if _SCALE_LABEL.fullmatch(normalized) else None
    score_value = fields.get("gen_ai.evaluation.score.value")
    if score_value is None:
        return label, None
    if not isinstance(score_value, int | float) or isinstance(score_value, bool):
        return None, None
    return label, float(score_value)


def _result_reference(context: _SubscriptionContext, event_id: str) -> str:
    material = f"{context.log_group}:{context.log_stream}:{event_id}".encode()
    return "result-" + hashlib.sha256(material).hexdigest()[:32]
