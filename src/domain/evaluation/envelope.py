# SPDX-License-Identifier: MIT-0
"""Exact post-release question/answer pairs admitted to the evaluation plane."""

from __future__ import annotations

import re
from dataclasses import dataclass

_CORRELATION = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_OPAQUE_REFERENCE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_MAX_GROUNDED_CONTEXT_BYTES = 192 * 1024
_MAX_EVALUATION_ENVELOPE_BYTES = 240 * 1024
_ENVELOPE_JSON_OVERHEAD_BYTES = 1024


@dataclass(frozen=True, slots=True)
class EvaluationEnvelope:
    """Exact accepted question and post-release answer admitted for evaluation."""

    question: str
    released_answer: str
    correlation_id: str
    audit_id: str
    evaluation_admission_ref: str
    grounded_context: str = ""

    def __post_init__(self) -> None:
        if not self.question.strip() or not self.released_answer.strip():
            raise ValueError("evaluation envelope requires a released question and answer.")
        if len(self.grounded_context.encode("utf-8")) > _MAX_GROUNDED_CONTEXT_BYTES:
            raise ValueError("evaluation envelope grounded context exceeds its bounded size.")
        aggregate_bytes = (
            sum(
                len(value.encode("utf-8"))
                for value in (
                    self.question,
                    self.released_answer,
                    self.correlation_id,
                    self.audit_id,
                    self.evaluation_admission_ref,
                    self.grounded_context,
                )
            )
            + _ENVELOPE_JSON_OVERHEAD_BYTES
        )
        if aggregate_bytes > _MAX_EVALUATION_ENVELOPE_BYTES:
            raise ValueError("evaluation envelope exceeds its aggregate delivery bound.")
        if not _CORRELATION.fullmatch(self.correlation_id):
            raise ValueError("evaluation envelope correlation must be a canonical UUID4.")
        for value, label in (
            (self.audit_id, "evaluation envelope audit ID"),
            (self.evaluation_admission_ref, "evaluation envelope admission reference"),
        ):
            if not _OPAQUE_REFERENCE.fullmatch(value):
                raise ValueError(f"{label} must be an opaque bounded reference.")
