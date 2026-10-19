# SPDX-License-Identifier: MIT-0
"""The closed vocabulary of the evaluation quality counts (ADR-079 §5).

AgentCore publishes a CloudWatch metric only for a verdict that carries a numeric score, so a
categorical evaluator such as grounded correctness has none [verified 2026-09-29, dev]. The
result linker therefore counts every linked verdict itself, by the evaluator's dimension and its
label. Both dimensions come from these closed sets, never from the service's text, so no value
outside them can become a metric dimension; anything else counts as `other`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

OTHER = "other"

QUALITY_LABELS: dict[str, frozenset[str]] = {
    "grounded_correctness": frozenset(
        {"grounded", "partially_grounded", "unsupported", "not_applicable"}
    ),
    "internal_consistency": frozenset({"consistent", "minor_issue", "contradictory"}),
    "helpfulness": frozenset({"excellent", "good", "needs_improvement", "poor"}),
}
"""Each V4 dimension's normalized scale labels. `infra/lib/stacks/evaluation-v4-stack.ts`
declares the scales; a test holds the two equal."""

_EVALUATOR_NAME = re.compile(
    r"^adlytics_[a-z0-9_]+?_(" + "|".join(QUALITY_LABELS) + r")_v4_[0-9a-f]{8}$"
)


@dataclass(frozen=True, slots=True)
class VerdictCount:
    """The two metric dimensions one linked verdict is counted under."""

    dimension: str
    label: str


def verdict_count(evaluator_name: str, label: str) -> VerdictCount:
    """Map a linked verdict to its closed dimension and label, or `other` for either."""
    matched = _EVALUATOR_NAME.fullmatch(evaluator_name)
    if matched is None:
        return VerdictCount(dimension=OTHER, label=OTHER)
    dimension = matched.group(1)
    return VerdictCount(
        dimension=dimension,
        label=label if label in QUALITY_LABELS[dimension] else OTHER,
    )
