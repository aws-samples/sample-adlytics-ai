# SPDX-License-Identifier: MIT-0
"""V2 conversation guardrail request boundary and adapter assessment tests."""

from typing import Any, cast

import pytest

from adapters.outbound.bedrock.guardrail import BedrockGuardrail
from ports.outbound.guardrail import Assessment, AssessmentRequest, Direction

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-041", "ADR-042")]


def test_guardrail_request_requires_bounded_text_and_correlation() -> None:
    request = AssessmentRequest(
        text="Review this fictional request.",
        direction=Direction.INBOUND,
        correlation_id="123e4567-e89b-42d3-a456-426614174000",
    )

    assert request.direction is Direction.INBOUND


@pytest.mark.parametrize(
    ("text", "correlation_id"),
    [("", "123e4567-e89b-42d3-a456-426614174000"), ("text", "")],
)
def test_guardrail_request_rejects_missing_boundary_fields(text: str, correlation_id: str) -> None:
    with pytest.raises(ValueError):
        AssessmentRequest(text=text, direction=Direction.OUTBOUND, correlation_id=correlation_id)


_CORRELATION = "123e4567-e89b-42d3-a456-426614174000"
_MATCHED = "fictional-person@example.invalid"


class _Client:
    """An `apply_guardrail` stand-in that returns one canned response."""

    def __init__(self, response: dict[str, object]) -> None:
        self.response = response

    def apply_guardrail(self, **_kwargs: object) -> dict[str, object]:
        return self.response


def _assess(response: dict[str, object]) -> Assessment:
    guardrail = BedrockGuardrail("gr-fictional", "DRAFT", client=cast("Any", _Client(response)))
    return guardrail.assess(
        AssessmentRequest(
            text="A fictional question.", direction=Direction.INBOUND, correlation_id=_CORRELATION
        )
    )


def test_an_intervention_names_each_blocking_check_and_never_the_matched_value() -> None:
    assessment = _assess(
        {
            "action": "GUARDRAIL_INTERVENED",
            "assessments": [
                {
                    "contentPolicy": {
                        "filters": [
                            {"type": "PROMPT_ATTACK", "confidence": "LOW", "action": "BLOCKED"},
                            {"type": "HATE", "confidence": "NONE", "action": "NONE"},
                        ]
                    },
                    "sensitiveInformationPolicy": {
                        "piiEntities": [
                            {"type": "EMAIL", "match": _MATCHED, "action": "BLOCKED"},
                            {"type": "NAME", "match": "Fictional", "action": "ANONYMIZED"},
                        ],
                        "regexes": [
                            {"name": "account-id", "match": "111122223333", "action": "BLOCKED"}
                        ],
                    },
                    "topicPolicy": {"topics": [{"name": "legal-advice", "action": "BLOCKED"}]},
                    "wordPolicy": {
                        "customWords": [{"match": "fictional-word", "action": "BLOCKED"}]
                    },
                }
            ],
        }
    )

    assert assessment.intervened is True
    assert assessment.signals == (
        "content:PROMPT_ATTACK:LOW",
        "pii:EMAIL",
        "regex:account-id",
        "topic:legal-advice",
        "word",
    )
    joined = " ".join(assessment.signals)
    for value in (_MATCHED, "111122223333", "fictional-word", "Fictional"):
        assert value not in joined
    assert "PROMPT_ATTACK" not in assessment.reason


def test_no_intervention_carries_no_signals_even_when_a_filter_reports() -> None:
    assessment = _assess(
        {
            "action": "NONE",
            "assessments": [
                {
                    "contentPolicy": {
                        "filters": [
                            {"type": "PROMPT_ATTACK", "confidence": "LOW", "action": "BLOCKED"}
                        ]
                    }
                }
            ],
        }
    )

    assert (assessment.intervened, assessment.signals) == (False, ())


def test_a_word_or_entity_that_did_not_block_is_not_a_signal() -> None:
    assessment = _assess(
        {
            "action": "GUARDRAIL_INTERVENED",
            "assessments": [
                {
                    "contentPolicy": {
                        "filters": [
                            {"type": "PROMPT_ATTACK", "confidence": "HIGH", "action": "BLOCKED"}
                        ]
                    },
                    "wordPolicy": {
                        "customWords": [{"match": "fictional-word", "action": "NONE"}],
                        "managedWordLists": [
                            {"match": "fictional", "type": "PROFANITY", "action": "NONE"}
                        ],
                    },
                    "sensitiveInformationPolicy": {
                        "piiEntities": [{"type": "NAME", "match": "Fictional", "action": "NONE"}]
                    },
                }
            ],
        }
    )

    assert assessment.signals == ("content:PROMPT_ATTACK:HIGH",)


def test_signals_are_deduplicated_and_bounded() -> None:
    filters = [{"type": f"T{i}", "confidence": "HIGH", "action": "BLOCKED"} for i in range(40)]
    repeated = [{"type": "EMAIL", "match": _MATCHED, "action": "BLOCKED"}] * 3
    assessment = _assess(
        {
            "action": "GUARDRAIL_INTERVENED",
            "assessments": [
                {"sensitiveInformationPolicy": {"piiEntities": repeated}},
                {"contentPolicy": {"filters": filters}},
            ],
        }
    )

    assert assessment.signals[0] == "pii:EMAIL"
    assert assessment.signals.count("pii:EMAIL") == 1
    assert len(assessment.signals) == 16
