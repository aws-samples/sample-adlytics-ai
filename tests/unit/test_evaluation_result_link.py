# SPDX-License-Identifier: MIT-0
"""Canonical content-free result-projection queue contracts."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from adapters.jobs.evaluation_result_link.work import result_from_json, result_to_json
from domain.evaluation.linkage import (
    EvaluationResultStateV1,
    EvaluationResultV3,
)
from domain.evaluation.release import EvaluationFamilyV1

pytestmark = pytest.mark.unit

_AUDIT = "audit-01"
_ADMISSION = "evaluation-admission-01"
_NOW = "2026-09-04T12:00:00+00:00"


def _result() -> EvaluationResultV3:
    return EvaluationResultV3(
        audit_id=_AUDIT,
        admission_ref=_ADMISSION,
        evaluator_ref="released-evaluator-01",
        evaluator_name="adlytics_dev_helpfulness_v4",
        result_ref="result-01",
        state=EvaluationResultStateV1.COMPLETED,
        label="good",
        score=0.75,
        evaluated_at=_NOW,
        family=EvaluationFamilyV1.V4,
        config_id="adlytics_dev_released_v4-0001",
        evaluator_revision="r1",
        model_id="fictional.judge-model-v1:0",
        prompt_digest="a" * 64,
    )


def test_projection_queue_round_trip_is_exact_and_contains_no_protected_content() -> None:
    encoded = result_to_json(_result())

    assert result_from_json(encoded) == _result()
    assert "question" not in encoded
    assert "answer" not in encoded
    assert "correlation" not in encoded
    assert "trace" not in encoded


def test_projection_queue_rejects_unknown_or_non_score_result_fields() -> None:
    document = json.loads(result_to_json(_result()))
    document["question"] = "must never enter the safe queue"
    with pytest.raises(ValueError):
        result_from_json(json.dumps(document))
    document = json.loads(result_to_json(_result()))
    document["state"] = "unavailable"
    document["label"] = None
    document["score"] = None
    with pytest.raises(ValueError):
        result_from_json(json.dumps(document))


@pytest.mark.covers("ADR-079")
def test_projection_queue_rejects_a_v2_body_without_its_provenance() -> None:
    """A result without the receipt's provenance must never become a stored verdict."""
    document = json.loads(result_to_json(_result()))
    for name in ("family", "config_id", "evaluator_revision", "model_id", "prompt_digest"):
        del document[name]
    document["schema_version"] = "EvaluationResult.v2"

    with pytest.raises(ValueError):
        result_from_json(json.dumps(document))


@pytest.mark.covers("ADR-082")
def test_a_completed_categorical_result_carries_a_label_and_no_score() -> None:
    """Grounded correctness is categorical, so its verdict is a label alone."""
    result = replace(_result(), label="not_applicable", score=None)
    assert result_from_json(result_to_json(result)) == result


@pytest.mark.covers("ADR-082")
def test_a_completed_result_still_requires_a_label_and_a_declared_score() -> None:
    with pytest.raises(ValueError, match="requires a label"):
        replace(_result(), label=None, score=None)
    with pytest.raises(ValueError, match="requires a label"):
        replace(_result(), label=None)
    with pytest.raises(ValueError, match="declared evaluator scale"):
        replace(_result(), score=0.1)
    with pytest.raises(ValueError, match="cannot retain"):
        replace(_result(), state=EvaluationResultStateV1.EVALUATOR_FAILED, score=None)
