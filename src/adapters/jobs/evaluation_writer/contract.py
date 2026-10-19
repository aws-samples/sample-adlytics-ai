# SPDX-License-Identifier: MIT-0
"""Pure V2 decoding and deterministic identifier derivation for released spans."""

from __future__ import annotations

import hashlib
import json
from typing import cast

from domain.evaluation.envelope import EvaluationEnvelope

_SCHEMA_V2 = 2
_SCHEMA_V3 = 3
_BASE_STRING_FIELDS: frozenset[str] = frozenset(
    {
        "audit_id",
        "correlation_id",
        "evaluation_admission_ref",
        "question",
        "released_answer",
    }
)
_V2_FIELDS: frozenset[str] = _BASE_STRING_FIELDS | {"schema_version"}
_V3_STRING_FIELDS: frozenset[str] = _BASE_STRING_FIELDS | {"grounded_context"}
_V3_FIELDS: frozenset[str] = _V3_STRING_FIELDS | {"schema_version"}


def decode_envelope(body: str) -> EvaluationEnvelope:
    """Decode the managed EvaluationEnvelope schema version 2 queue contract."""
    raw: object = json.loads(body)
    if not isinstance(raw, dict):
        raise ValueError("evaluation queue envelope must be an object")
    document = cast("dict[str, object]", raw)
    fields = frozenset(document)
    version = document.get("schema_version")
    if version == _SCHEMA_V2 and fields == _V2_FIELDS:
        string_fields = _BASE_STRING_FIELDS
        grounded_context = ""
    elif version == _SCHEMA_V3 and fields == _V3_FIELDS:
        string_fields = _V3_STRING_FIELDS
        grounded_context = document["grounded_context"]
    else:
        raise ValueError("evaluation queue envelope has unknown, missing, or unsupported fields")
    values = {field: document[field] for field in string_fields}
    if not all(isinstance(value, str) for value in values.values()):
        raise ValueError("evaluation queue envelope fields must be strings")
    return EvaluationEnvelope(
        question=cast("str", values["question"]),
        released_answer=cast("str", values["released_answer"]),
        correlation_id=cast("str", values["correlation_id"]),
        audit_id=cast("str", values["audit_id"]),
        evaluation_admission_ref=cast("str", values["evaluation_admission_ref"]),
        grounded_context=cast("str", grounded_context),
    )


def released_span_identifiers(envelope: EvaluationEnvelope) -> tuple[str, str]:
    """Derive released evaluation trace/span identifiers solely from admission custody."""
    identity = envelope.evaluation_admission_ref.encode()
    return (
        hashlib.sha256(b"released-evaluation-trace:" + identity).hexdigest()[:32],
        hashlib.sha256(b"released-evaluation-span:" + identity).hexdigest()[:16],
    )
