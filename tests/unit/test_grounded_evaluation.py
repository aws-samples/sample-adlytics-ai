# SPDX-License-Identifier: MIT-0
"""The evidence and answer obligations evaluation judges a streamed answer against (ADR-083)."""

import json

import pytest
from governed_evidence_fixture import FIXTURE_TOOL_RESULT_REF, shaped_response

from domain.conversation.grounded_evaluation import (
    GROUNDED_EVALUATION_CONTEXT_VERSION,
    AnswerObligationsV1,
    answer_obligations,
    grounded_evaluation_context,
)
from domain.conversation.tool_custody import (
    AgentToolCustodyV1,
    ToolCustodyDispositionV1,
    ValidatedToolResultV1,
    evaluation_grounding_sets,
)
from domain.narrative.publication import (
    CollectionCompletenessV1,
    GroundedCollectionV1,
    GroundedRendering,
    GroundingSet,
    QualifierKind,
    RenderingKind,
    grounded_refusal,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-083")]

_METRIC = "grounding-fictional-metric"
_REFUSAL_REASON = "A fictional metric is declared but its facts are not built yet."


def _metric_grounding() -> GroundingSet:
    """Build a claim, a mandatory qualifier, and one required and one optional collection."""
    return GroundingSet(
        grounding_id=_METRIC,
        renderings=(
            GroundedRendering(
                rendering_id=f"{_METRIC}:row:0:count",
                text="Fictional count was 7 on 2026-01-01.",
                kind=RenderingKind.CLAIM,
                source_paths=("data.values[0].count",),
            ),
            GroundedRendering(
                rendering_id=f"{_METRIC}:stale",
                text="Fictional data was last refreshed a week ago.",
                kind=RenderingKind.QUALIFIER,
                source_paths=("metadata.freshness_status",),
                qualifier=QualifierKind.STALE,
            ),
        ),
        mandatory_rendering_ids=(f"{_METRIC}:stale",),
        requires_claim_selection=True,
        collections=(
            GroundedCollectionV1(
                collection_id=f"{_METRIC}:campaigns",
                heading="Fictional campaigns",
                items=("Campaign Alpha", "Campaign Beta"),
                completeness=CollectionCompletenessV1.COMPLETE,
                source_paths=("data.collections[0]",),
                required=True,
                count_label="campaigns",
            ),
            GroundedCollectionV1(
                collection_id=f"{_METRIC}:examples",
                heading="Fictional examples",
                items=("Example One",),
                completeness=CollectionCompletenessV1.TOP_N,
                source_paths=("data.collections[1]",),
            ),
        ),
    )


def _refusal_grounding() -> GroundingSet:
    return grounded_refusal(_REFUSAL_REASON, blocks_answer=True, identity_salt="fictional")


def test_answer_obligations_are_derived_only_from_tool_evidence() -> None:
    refusal = _refusal_grounding()

    obligations = answer_obligations((_metric_grounding(), refusal))

    assert obligations == AnswerObligationsV1(
        required_rendering_ids=(f"{_METRIC}:stale", refusal.mandatory_rendering_ids[0]),
        required_collection_ids=(f"{_METRIC}:campaigns",),
        claim_required_grounding_ids=(_METRIC,),
    )


def test_a_turn_without_evidence_has_no_obligations() -> None:
    assert answer_obligations(()) == AnswerObligationsV1(
        required_rendering_ids=(), required_collection_ids=(), claim_required_grounding_ids=()
    )


@pytest.mark.parametrize(
    "references",
    [("fictional-id", "fictional-id"), (" ",)],
)
def test_answer_obligations_require_unique_non_empty_references(
    references: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError, match="unique non-empty references"):
        AnswerObligationsV1(
            required_rendering_ids=(),
            required_collection_ids=references,
            claim_required_grounding_ids=(),
        )


def test_the_context_document_carries_obligations_and_evidence_and_no_verification() -> None:
    refusal = _refusal_grounding()

    document = json.loads(grounded_evaluation_context((_metric_grounding(), refusal)))

    assert set(document) == {"schema_version", "obligations", "evidence"}
    assert document["schema_version"] == GROUNDED_EVALUATION_CONTEXT_VERSION
    assert GROUNDED_EVALUATION_CONTEXT_VERSION == "GroundedEvaluationContext.v2"
    assert document["obligations"] == {
        "schema_version": "AnswerObligations.v1",
        "required_rendering_ids": [f"{_METRIC}:stale", refusal.mandatory_rendering_ids[0]],
        "required_collection_ids": [f"{_METRIC}:campaigns"],
        "claim_required_grounding_ids": [_METRIC],
    }
    metric, refused = document["evidence"]
    assert metric == {
        "grounding_id": _METRIC,
        "renderings": [
            {
                "rendering_id": f"{_METRIC}:row:0:count",
                "text": "Fictional count was 7 on 2026-01-01.",
                "kind": "claim",
                "source_paths": ["data.values[0].count"],
                "qualifier": None,
            },
            {
                "rendering_id": f"{_METRIC}:stale",
                "text": "Fictional data was last refreshed a week ago.",
                "kind": "qualifier",
                "source_paths": ["metadata.freshness_status"],
                "qualifier": "stale",
            },
        ],
        "collections": [
            {
                "collection_id": f"{_METRIC}:campaigns",
                "heading": "Fictional campaigns",
                "items": ["Campaign Alpha", "Campaign Beta"],
                "count": 2,
                "completeness": "complete",
                "required": True,
            },
            {
                "collection_id": f"{_METRIC}:examples",
                "heading": "Fictional examples",
                "items": ["Example One"],
                "count": 1,
                "completeness": "top_n",
                "required": False,
            },
        ],
    }
    assert refused["grounding_id"] == refusal.grounding_id
    assert refused["renderings"][0]["kind"] == "refusal"
    assert refused["renderings"][0]["text"] == _REFUSAL_REASON
    assert refused["collections"] == []


def test_the_context_document_is_canonical_json() -> None:
    groundings = (_metric_grounding(), _refusal_grounding())

    serialized = grounded_evaluation_context(groundings)

    assert serialized == grounded_evaluation_context(groundings)
    assert serialized == json.dumps(
        json.loads(serialized), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )


def test_a_turn_without_evidence_still_produces_a_context_document() -> None:
    document = json.loads(grounded_evaluation_context(()))

    assert document["evidence"] == []
    assert document["obligations"]["required_rendering_ids"] == []


@pytest.mark.covers("ADR-062")
def test_a_superseded_refusal_stays_evidence_but_is_no_longer_an_obligation() -> None:
    """The model adapted around a dead end; evaluation still sees it but does not require it."""
    superseded = grounded_refusal(
        "A fictional grain constraint refused the first attempt.",
        blocks_answer=False,
        identity_salt="superseded",
    )
    success = shaped_response().metadata.grounding
    assert success is not None
    custody = AgentToolCustodyV1.from_results(
        (
            ValidatedToolResultV1(
                tool_use_ref="use-1",
                tool_name="measurement_metric_by_grain",
                ordinal=1,
                disposition=ToolCustodyDispositionV1.REFUSED,
                tool_result_ref="11111111-1111-4111-8111-111111111111",
                grounding_set=superseded,
                refusal_reason="A fictional grain constraint refused the first attempt.",
            ),
            ValidatedToolResultV1(
                tool_use_ref="use-2",
                tool_name="measurement_metric_by_grain",
                ordinal=2,
                disposition=ToolCustodyDispositionV1.SUCCEEDED,
                tool_result_ref=FIXTURE_TOOL_RESULT_REF,
                grounding_set=success,
            ),
        )
    )

    document = json.loads(grounded_evaluation_context(evaluation_grounding_sets(custody)))

    assert (
        superseded.mandatory_rendering_ids[0]
        not in (document["obligations"]["required_rendering_ids"])
    )
    assert document["obligations"]["claim_required_grounding_ids"] == [success.grounding_id]
    assert [item["grounding_id"] for item in document["evidence"]] == [
        superseded.grounding_id,
        success.grounding_id,
    ]
    assert document["evidence"][0]["renderings"][0]["text"] == (
        "A fictional grain constraint refused the first attempt."
    )
