# SPDX-License-Identifier: MIT-0
"""The closed vocabulary of the evaluation quality counts (ADR-079 §5). Values are fictional."""

from __future__ import annotations

import pytest

from domain.evaluation.quality import OTHER, QUALITY_LABELS, VerdictCount, verdict_count

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-079")]


@pytest.mark.parametrize(
    ("evaluator_name", "label", "expected"),
    [
        (
            "adlytics_dev_grounded_correctness_v4_9c816d6a",
            "unsupported",
            ("grounded_correctness", "unsupported"),
        ),
        (
            "adlytics_dev_internal_consistency_v4_f14af25d",
            "minor_issue",
            ("internal_consistency", "minor_issue"),
        ),
        ("adlytics_acme_prod_helpfulness_v4_0123abcd", "poor", ("helpfulness", "poor")),
    ],
)
def test_a_current_evaluator_counts_under_its_dimension_and_label(
    evaluator_name: str, label: str, expected: tuple[str, str]
) -> None:
    assert verdict_count(evaluator_name, label) == VerdictCount(*expected)


@pytest.mark.parametrize(
    ("evaluator_name", "label", "expected"),
    [
        # A label the scale does not declare never becomes a dimension value.
        ("adlytics_dev_helpfulness_v4_4576eb06", "fictional_new_label", ("helpfulness", OTHER)),
        # A retired or unrevisioned name counts nowhere real.
        ("adlytics_dev_helpfulness_v4", "good", (OTHER, OTHER)),
        ("adlytics_dev_released_quality_v3_7cda21c4", "excellent", (OTHER, OTHER)),
    ],
)
def test_anything_outside_the_vocabulary_counts_as_other(
    evaluator_name: str, label: str, expected: tuple[str, str]
) -> None:
    assert verdict_count(evaluator_name, label) == VerdictCount(*expected)


def test_every_dimension_declares_its_worst_label() -> None:
    worst = {
        "grounded_correctness": "unsupported",
        "internal_consistency": "contradictory",
        "helpfulness": "poor",
    }
    for dimension, label in worst.items():
        assert label in QUALITY_LABELS[dimension]
