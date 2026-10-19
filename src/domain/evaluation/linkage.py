# SPDX-License-Identifier: MIT-0
"""Content-free immutable linkage contracts for protected online evaluation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from domain.evaluation.outbox import evaluation_delivery_ref
from domain.evaluation.release import (
    EvaluationFamilyV1,
    EvaluationReleaseEvaluatorV1,
    require_unique_evaluators,
)

_OPAQUE_REFERENCE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID = re.compile(r"^[0-9a-f]{16}$")
_ALLOWED_SCORES = frozenset({0.0, 0.5, 0.75, 1.0})
_SCALE_LABEL = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _opaque(value: str, label: str) -> None:
    if not _OPAQUE_REFERENCE.fullmatch(value):
        raise ValueError(f"{label} must be an opaque bounded reference.")


def _instant(value: str, label: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{label} must be an ISO instant.") from None
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError(f"{label} must be a UTC ISO instant.")


class EvaluationResultStateV1(StrEnum):
    """Closed outcomes retained from one protected evaluator result event."""

    COMPLETED = "completed"
    EVALUATOR_FAILED = "evaluator_failed"


@dataclass(frozen=True, slots=True, kw_only=True)
class EvaluationAdmissionV1:
    """A selected protected evaluation linked to one immutable audit receipt."""

    schema_version: Literal["EvaluationAdmission.v1"] = "EvaluationAdmission.v1"
    audit_id: str
    admission_ref: str
    selected_at: str

    def __post_init__(self) -> None:
        _opaque(self.audit_id, "evaluation admission audit ID")
        _opaque(self.admission_ref, "evaluation admission reference")
        _instant(self.selected_at, "evaluation admission selection time")


class EvaluationDeliveryOutcomeV1(StrEnum):
    """Whether the dispatcher sent one admitted turn to evaluation (ADR-079)."""

    DISPATCHED = "dispatched"
    NOT_DISPATCHED = "not_dispatched"


class EvaluationNotDispatchedReasonV1(StrEnum):
    """The closed reasons a row ends without being sent to any evaluator."""

    EVALUATION_NOT_ENABLED = "evaluation_not_enabled"
    ANSWER_NOT_RECEIVED = "answer_not_received"
    DELIVERY_UNKNOWN = "delivery_unknown"


@dataclass(frozen=True, slots=True, kw_only=True)
class EvaluationDeliveryFamilyV1:
    """One enabled family a row was sent to, with the evaluator set released at that run."""

    family: EvaluationFamilyV1
    config_id: str
    evaluators: tuple[EvaluationReleaseEvaluatorV1, ...]

    def __post_init__(self) -> None:
        _opaque(self.config_id, "evaluation delivery configuration ID")
        if not self.evaluators:
            raise ValueError("an evaluation delivery family requires at least one evaluator.")
        require_unique_evaluators(self.evaluators, "evaluation delivery family")

    def evaluator(self, evaluator_id: str) -> EvaluationReleaseEvaluatorV1 | None:
        """Return the released evaluator with this ID, or `None` when it was not sent to it."""
        return next((item for item in self.evaluators if item.evaluator_id == evaluator_id), None)


@dataclass(frozen=True, slots=True, kw_only=True)
class EvaluationDeliveryReceiptV2:
    """The dispatcher's content-free decision for one admitted turn (ADR-079).

    Dispatched names every family the row was sent to and nothing else; not dispatched names
    its reason and no family, so a verdict can only be stamped from a family that was sent.
    """

    schema_version: Literal["EvaluationDeliveryReceipt.v2"] = "EvaluationDeliveryReceipt.v2"
    audit_id: str
    admission_ref: str
    delivery_ref: str
    decided_at: str
    outcome: EvaluationDeliveryOutcomeV1
    not_dispatched_reason: EvaluationNotDispatchedReasonV1 | None
    families: tuple[EvaluationDeliveryFamilyV1, ...]

    def __post_init__(self) -> None:
        _opaque(self.audit_id, "evaluation delivery audit ID")
        _opaque(self.admission_ref, "evaluation delivery admission reference")
        if self.delivery_ref != evaluation_delivery_ref(self.audit_id, self.admission_ref):
            raise ValueError("evaluation delivery reference must be deterministic.")
        _instant(self.decided_at, "evaluation delivery decision time")
        if self.outcome is EvaluationDeliveryOutcomeV1.DISPATCHED:
            if not self.families or self.not_dispatched_reason is not None:
                raise ValueError(
                    "a dispatched evaluation delivery requires families and no reason."
                )
        elif self.families or self.not_dispatched_reason is None:
            raise ValueError("an undispatched evaluation delivery requires only its reason.")
        names = [item.family for item in self.families]
        if len(set(names)) != len(names):
            raise ValueError("evaluation delivery families must be unique.")

    def dispatched_family(self, config_id: str) -> EvaluationDeliveryFamilyV1 | None:
        """Return the family sent under this configuration, or `None` when none was."""
        if self.outcome is not EvaluationDeliveryOutcomeV1.DISPATCHED:
            return None
        return next((item for item in self.families if item.config_id == config_id), None)


@dataclass(frozen=True, slots=True, kw_only=True)
class EvaluationWriterReceiptV1:
    """Content-free evidence that the protected input span was emitted once logically."""

    schema_version: Literal["EvaluationWriterReceipt.v1"] = "EvaluationWriterReceipt.v1"
    audit_id: str
    admission_ref: str
    trace_id: str
    span_id: str
    emitted_at: str

    def __post_init__(self) -> None:
        _opaque(self.audit_id, "evaluation writer audit ID")
        _opaque(self.admission_ref, "evaluation writer admission reference")
        if not _TRACE_ID.fullmatch(self.trace_id):
            raise ValueError("evaluation writer trace ID must be a 32-character lowercase hex ID.")
        if not _SPAN_ID.fullmatch(self.span_id):
            raise ValueError("evaluation writer span ID must be a 16-character lowercase hex ID.")
        _instant(self.emitted_at, "evaluation writer emission time")


@dataclass(frozen=True, slots=True, kw_only=True)
class EvaluationResultV3:
    """The allowlisted result of one evaluator over one admitted turn, with its provenance.

    One admission is judged by several evaluators, so the result is keyed by evaluator as well
    as admission. A completed result is recorded by its label: the evaluator's own scale label,
    normalized, because the scale belongs to the evaluator definition rather than to this
    contract. Its score is present only when the evaluator's scale is numerical, and then stays
    within the union of declared numerical values; a categorical scale, such as grounded
    correctness with its Not Applicable option, records a label and no score (ADR-082). A result
    in any other state keeps neither. The family, configuration, revision, model, and prompt digest
    come from the turn's delivery receipt, so a late result keeps the revision it was produced
    under (ADR-079).
    """

    schema_version: Literal["EvaluationResult.v3"] = "EvaluationResult.v3"
    audit_id: str
    admission_ref: str
    evaluator_ref: str
    evaluator_name: str
    result_ref: str
    state: EvaluationResultStateV1
    label: str | None
    score: float | None
    evaluated_at: str
    family: EvaluationFamilyV1
    config_id: str
    evaluator_revision: str
    model_id: str
    prompt_digest: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.audit_id, "evaluation result audit ID"),
            (self.admission_ref, "evaluation result admission reference"),
            (self.evaluator_ref, "evaluation result evaluator reference"),
            (self.evaluator_name, "evaluation result evaluator name"),
            (self.result_ref, "evaluation result reference"),
            (self.config_id, "evaluation result configuration ID"),
        ):
            _opaque(value, label)
        # The provenance shape is the released evaluator's; validate it by constructing one.
        EvaluationReleaseEvaluatorV1(
            evaluator_id=self.evaluator_ref,
            evaluator_name=self.evaluator_name,
            revision=self.evaluator_revision,
            model_id=self.model_id,
            prompt_digest=self.prompt_digest,
        )
        if self.state is EvaluationResultStateV1.COMPLETED:
            if self.label is None:
                raise ValueError("completed evaluation result requires a label.")
            if not _SCALE_LABEL.fullmatch(self.label):
                raise ValueError("evaluation result label must be a normalized scale label.")
            if self.score is not None and (
                isinstance(self.score, bool) or float(self.score) not in _ALLOWED_SCORES
            ):
                raise ValueError("evaluation result score is not in the declared evaluator scale.")
        elif self.label is not None or self.score is not None:
            raise ValueError("non-score evaluation result cannot retain a label or score.")
        _instant(self.evaluated_at, "evaluation result time")


def normalize_scale_label(value: str) -> str:
    """Fold one evaluator scale label such as "Partially Grounded" into its stored form."""
    return "_".join(value.strip().lower().split())
