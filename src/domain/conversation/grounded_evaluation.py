# SPDX-License-Identifier: MIT-0
"""The trusted evidence and answer obligations evaluation judges a released answer against.

Nothing here gates release (ADR-083). Governed tools return a grounding projection with each
result: server-formatted claims, qualifiers, refusals, and collections, plus which of them an
answer must carry. The grounded-correctness evaluator reads this document to decide whether the
streamed answer's platform and customer claims are supported and whether it carried every
mandatory qualifier and required collection.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Literal

from domain.narrative.publication import GroundingSet

__all__ = ["AnswerObligationsV1", "answer_obligations", "grounded_evaluation_context"]

GROUNDED_EVALUATION_CONTEXT_VERSION = "GroundedEvaluationContext.v2"


@dataclass(frozen=True, slots=True, kw_only=True)
class AnswerObligationsV1:
    """Server-derived factual requirements for one current-turn answer."""

    schema_version: Literal["AnswerObligations.v1"] = "AnswerObligations.v1"
    required_rendering_ids: tuple[str, ...]
    required_collection_ids: tuple[str, ...]
    claim_required_grounding_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for values in (
            self.required_rendering_ids,
            self.required_collection_ids,
            self.claim_required_grounding_ids,
        ):
            if len(set(values)) != len(values) or any(not item.strip() for item in values):
                raise ValueError("answer obligations require unique non-empty references.")


def answer_obligations(grounding_sets: tuple[GroundingSet, ...]) -> AnswerObligationsV1:
    """Derive what a complete answer must carry, exclusively from trusted tool evidence."""
    return AnswerObligationsV1(
        required_rendering_ids=tuple(
            rendering_id
            for grounding in grounding_sets
            for rendering_id in grounding.mandatory_rendering_ids
        ),
        required_collection_ids=tuple(
            collection.collection_id
            for grounding in grounding_sets
            for collection in grounding.collections
            if collection.required
        ),
        claim_required_grounding_ids=tuple(
            grounding.grounding_id
            for grounding in grounding_sets
            if grounding.requires_claim_selection
        ),
    )


def grounded_evaluation_context(grounding_sets: tuple[GroundingSet, ...]) -> str:
    """Project bounded authoritative facts and obligations for protected evaluation only."""
    evidence = [
        {
            "grounding_id": grounding.grounding_id,
            "renderings": [
                {
                    "rendering_id": item.rendering_id,
                    "text": item.text,
                    "kind": item.kind.value,
                    "source_paths": list(item.source_paths),
                    "qualifier": None if item.qualifier is None else item.qualifier.value,
                }
                for item in grounding.renderings
            ],
            "collections": [
                {
                    "collection_id": item.collection_id,
                    "heading": item.heading,
                    "items": list(item.items),
                    "count": len(item.items),
                    "completeness": item.completeness.value,
                    "required": item.required,
                }
                for item in grounding.collections
            ],
        }
        for grounding in grounding_sets
    ]
    return json.dumps(
        {
            "schema_version": GROUNDED_EVALUATION_CONTEXT_VERSION,
            "obligations": asdict(answer_obligations(grounding_sets)),
            "evidence": evidence,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
