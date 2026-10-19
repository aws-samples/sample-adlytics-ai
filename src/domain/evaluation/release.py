# SPDX-License-Identifier: MIT-0
"""The release record: whether one evaluation family is enabled, and under what (ADR-079).

Each family's gate is the only writer of its release record. The dispatcher reads it on every
run and sends nothing to a family it does not name enabled; the delivery receipt then copies the
evaluator set the row was sent to, so a verdict's provenance never depends on a later record.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final, Literal

_OPAQUE_REFERENCE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_MODEL_ID = re.compile(r"^[A-Za-z0-9._:/-]{1,256}$")
"""A foundation-model ID, inference-profile ID, or ARN; it is never used in a key."""
_PROMPT_DIGEST = re.compile(r"^[0-9a-f]{64}$")


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


class EvaluationFamilyV1(StrEnum):
    """The closed set of evaluation families a stored record may name.

    Only `CURRENT_EVALUATION_FAMILIES` have a gate and a release record.
    """

    V3 = "v3"
    """Retired 2026-09-27 (ADR-079). Kept only so that stored delivery receipts and verdicts
    written while V3 ran still decode; remove it once they have expired (2026-12-26 in dev)."""
    V4 = "v4"


CURRENT_EVALUATION_FAMILIES: Final[tuple[EvaluationFamilyV1, ...]] = (EvaluationFamilyV1.V4,)
"""The families a gate specification, release record, or release parameter may name."""


def require_current_family(family: EvaluationFamilyV1, label: str) -> None:
    """Refuse a family that is retired: it has no gate, release record, or configuration."""
    if family not in CURRENT_EVALUATION_FAMILIES:
        raise ValueError(f"{label} names the retired evaluation family {family!s}.")


@dataclass(frozen=True, slots=True, kw_only=True)
class EvaluationReleaseEvaluatorV1:
    """One released evaluator and the provenance every verdict it produces carries."""

    evaluator_id: str
    evaluator_name: str
    revision: str
    model_id: str
    prompt_digest: str
    """SHA-256 of the evaluator's prompt, as 64 lowercase hex characters."""

    def __post_init__(self) -> None:
        _opaque(self.evaluator_id, "released evaluator ID")
        _opaque(self.evaluator_name, "released evaluator name")
        _opaque(self.revision, "released evaluator revision")
        if not _MODEL_ID.fullmatch(self.model_id):
            raise ValueError("released evaluator model ID must be a bounded model identifier.")
        if not _PROMPT_DIGEST.fullmatch(self.prompt_digest):
            raise ValueError("released evaluator prompt digest must be 64 lowercase hex.")


def require_unique_evaluators(
    evaluators: tuple[EvaluationReleaseEvaluatorV1, ...], label: str
) -> None:
    """Refuse an evaluator set that names one evaluator twice; ingress matches by ID."""
    identities = [evaluator.evaluator_id for evaluator in evaluators]
    if len(set(identities)) != len(identities):
        raise ValueError(f"{label} evaluator IDs must be unique.")


@dataclass(frozen=True, slots=True, kw_only=True)
class EvaluationReleaseV1:
    """One family's release record; a missing record reads as not enabled.

    Enabled names exactly one configuration and at least one evaluator. Not enabled names
    neither, so a disabled record can never carry a stale evaluator set forward.
    """

    schema_version: Literal["EvaluationRelease.v1"] = "EvaluationRelease.v1"
    family: EvaluationFamilyV1
    enabled: bool
    config_id: str | None
    evaluators: tuple[EvaluationReleaseEvaluatorV1, ...]
    decided_at: str

    def __post_init__(self) -> None:
        require_current_family(self.family, "an evaluation release")
        if self.enabled:
            if self.config_id is None or not self.evaluators:
                raise ValueError(
                    "an enabled evaluation release requires its configuration and evaluators."
                )
            _opaque(self.config_id, "evaluation release configuration ID")
        elif self.config_id is not None or self.evaluators:
            raise ValueError(
                "a disabled evaluation release cannot name a configuration or evaluators."
            )
        require_unique_evaluators(self.evaluators, "evaluation release")
        _instant(self.decided_at, "evaluation release decision time")
