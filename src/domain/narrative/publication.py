# SPDX-License-Identifier: MIT-0
"""Grounded answer-publication protocol for deterministic numerical narration.

ADR-049. A model selects domain-authored rendering IDs and must leave connective text empty.
The release boundary inserts the selected renderings verbatim. Added, altered, recomputed,
unqualified, or model-authored prose therefore has no representation that can be published.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "AnswerPublicationDecision",
    "AnswerPublicationRequest",
    "AnswerQualifier",
    "CandidateNarration",
    "CollectionCompletenessV1",
    "GroundedCollectionV1",
    "GroundedRendering",
    "GroundingSet",
    "QualifierKind",
    "RenderingKind",
    "grounded_refusal",
    "validate_answer_publication",
]

_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]*$")


class RenderingKind(StrEnum):
    """Why one deterministic rendering exists in the grounding set."""

    CLAIM = "claim"
    QUALIFIER = "qualifier"
    CONTEXT = "context"
    RECOMMENDATION = "recommendation"
    REFUSAL = "refusal"


class CollectionCompletenessV1(StrEnum):
    """Whether a governed collection may be presented as exhaustive."""

    COMPLETE = "complete"
    SELECTED = "selected"
    TOP_N = "top_n"
    TRUNCATED = "truncated"
    SUPPRESSED = "suppressed"
    UNKNOWN = "unknown"


class QualifierKind(StrEnum):
    """Closed caveats whose omission changes the meaning of a figure."""

    STALE = "stale"
    PARTIAL = "partial"
    SUPPRESSED = "suppressed"
    MIXED_BASIS = "mixed_basis"
    PERIODIC = "periodic"
    TRUNCATED = "truncated"
    QUALIFIED_DQ = "qualified_dq"
    QUARANTINED = "quarantined"
    SOURCE_TIMEZONE = "source_timezone"
    ASSOCIATION_ONLY = "association_only"
    """A `causal: false` metric (ADR-102): the answer says "association, not cause"."""


@dataclass(frozen=True, slots=True)
class AnswerQualifier:
    """Typed semantic caveat and its only permitted visible rendering."""

    kind: QualifierKind
    rendering: str
    source_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.rendering.strip() or not self.source_paths:
            raise ValueError("answer qualifier requires rendering and typed source paths.")


@dataclass(frozen=True, slots=True)
class GroundedRendering:
    """One exact domain-authored statement the model may select, never rewrite."""

    rendering_id: str
    text: str
    kind: RenderingKind
    source_paths: tuple[str, ...]
    qualifier: QualifierKind | None = None

    def __post_init__(self) -> None:
        if not _ID.fullmatch(self.rendering_id.strip()):
            raise ValueError(f"grounded rendering id is invalid: {self.rendering_id!r}.")
        if not self.text.strip() or not self.source_paths:
            raise ValueError("grounded rendering requires text and source paths.")
        if (self.kind is RenderingKind.QUALIFIER) != (self.qualifier is not None):
            raise ValueError("only qualifier renderings carry a qualifier kind.")


@dataclass(frozen=True, slots=True)
class GroundedCollectionV1:
    """One tool-owned ordered set whose membership and count cannot be model-authored."""

    collection_id: str
    heading: str
    items: tuple[str, ...]
    completeness: CollectionCompletenessV1
    source_paths: tuple[str, ...]
    required: bool = False
    count_label: str = "items"

    def __post_init__(self) -> None:
        if not _ID.fullmatch(self.collection_id.strip()):
            raise ValueError(f"grounded collection id is invalid: {self.collection_id!r}.")
        if (
            not self.heading.strip()
            or not self.items
            or any(not item.strip() for item in self.items)
        ):
            raise ValueError("grounded collection requires a heading and non-empty items.")
        if len(set(self.items)) != len(self.items):
            raise ValueError("grounded collection repeats a public item.")
        if not self.source_paths or any(not path.strip() for path in self.source_paths):
            raise ValueError("grounded collection requires typed source paths.")
        if not self.count_label.strip():
            raise ValueError("grounded collection count label is required.")
        if self.required and self.completeness is not CollectionCompletenessV1.COMPLETE:
            raise ValueError("a required grounded collection must be complete.")


@dataclass(frozen=True, slots=True)
class GroundingSet:
    """Authoritative factual atoms and collections for one exact tool response."""

    grounding_id: str
    renderings: tuple[GroundedRendering, ...]
    mandatory_rendering_ids: tuple[str, ...]
    requires_claim_selection: bool
    collections: tuple[GroundedCollectionV1, ...] = ()

    def __post_init__(self) -> None:
        if not _ID.fullmatch(self.grounding_id.strip()):
            raise ValueError(f"grounding id is invalid: {self.grounding_id!r}.")
        ids = tuple(rendering.rendering_id for rendering in self.renderings)
        if len(set(ids)) != len(ids):
            raise ValueError("grounding set repeats a rendering id.")
        if len(set(self.mandatory_rendering_ids)) != len(self.mandatory_rendering_ids):
            raise ValueError("grounding set repeats a mandatory rendering id.")
        unknown = set(self.mandatory_rendering_ids) - set(ids)
        if unknown:
            raise ValueError("grounding set requires an unknown rendering id.")
        qualifier_ids = {
            rendering.rendering_id
            for rendering in self.renderings
            if rendering.kind is RenderingKind.QUALIFIER
        }
        if not qualifier_ids <= set(self.mandatory_rendering_ids):
            raise ValueError("every semantic qualifier must be mandatory at release.")
        collection_ids = tuple(collection.collection_id for collection in self.collections)
        if len(set(collection_ids)) != len(collection_ids):
            raise ValueError("grounding set repeats a collection id.")


def grounded_refusal(
    reason: str,
    *,
    blocks_answer: bool = False,
    identity_salt: str = "",
) -> GroundingSet:
    """Create one exact domain-authored refusal the model may select but never rewrite."""
    if not reason.strip():
        raise ValueError("grounded refusal requires a safe reason.")
    identity = hashlib.sha256(f"{reason}|{identity_salt}".encode()).hexdigest()[:32]
    grounding_id = f"grounding-refusal-{identity}"
    rendering_id = f"{grounding_id}:reason"
    return GroundingSet(
        grounding_id=grounding_id,
        renderings=(
            GroundedRendering(
                rendering_id=rendering_id,
                text=reason,
                kind=(RenderingKind.REFUSAL if blocks_answer else RenderingKind.CONTEXT),
                source_paths=("refusal.reason",),
            ),
        ),
        mandatory_rendering_ids=(rendering_id,),
        requires_claim_selection=False,
    )


@dataclass(frozen=True, slots=True)
class CandidateNarration:
    """Model-authored rendering selection; connective text must remain empty."""

    connective_text: str
    rendering_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if len(set(self.rendering_ids)) != len(self.rendering_ids):
            raise ValueError("candidate narration repeats a rendering id.")


@dataclass(frozen=True, slots=True)
class AnswerPublicationRequest:
    """Candidate selection and every authoritative grounding set from this turn."""

    candidate: CandidateNarration
    grounding_sets: tuple[GroundingSet, ...]

    def __post_init__(self) -> None:
        ids = tuple(item.grounding_id for item in self.grounding_sets)
        if len(set(ids)) != len(ids):
            raise ValueError("answer publication repeats a grounding set.")


@dataclass(frozen=True, slots=True)
class AnswerPublicationDecision:
    """Released deterministic text, or a safe refusal reason and no text."""

    released_text: str | None
    refusal_reason: str | None = None

    def __post_init__(self) -> None:
        if (self.released_text is None) == (self.refusal_reason is None):
            raise ValueError("answer publication decision must release or refuse, exclusively.")

    @property
    def released(self) -> bool:
        """Whether candidate selection survived every grounding invariant."""
        return self.released_text is not None


def _refused(reason: str) -> AnswerPublicationDecision:
    return AnswerPublicationDecision(
        released_text=None,
        refusal_reason=f"The answer was withheld because {reason}",
    )


def _missing_claim_selection(
    grounding_sets: tuple[GroundingSet, ...], selected: tuple[str, ...]
) -> str | None:
    for grounding in grounding_sets:
        claim_ids = {
            item.rendering_id for item in grounding.renderings if item.kind is RenderingKind.CLAIM
        }
        if grounding.requires_claim_selection and not claim_ids.intersection(selected):
            return grounding.grounding_id
    return None


def _blocking_refusal(grounding_sets: tuple[GroundingSet, ...]) -> str | None:
    renderings = tuple(
        rendering.text
        for grounding in grounding_sets
        for rendering in grounding.renderings
        if rendering.kind is RenderingKind.REFUSAL
    )
    return "\n".join(renderings) if renderings else None


def _candidate_refusal(request: AnswerPublicationRequest) -> str | None:
    if request.candidate.connective_text.strip():
        return "model-authored prose is not a releasable grounded statement."
    if not request.grounding_sets:
        return "no structured grounding evidence reached the release boundary."
    return None


def validate_answer_publication(
    request: AnswerPublicationRequest,
) -> AnswerPublicationDecision:
    """Release only selected exact renderings with every mandatory item present."""
    candidate = request.candidate
    blocking_refusal = _blocking_refusal(request.grounding_sets)
    if blocking_refusal is not None:
        return AnswerPublicationDecision(released_text=blocking_refusal)
    reason = _candidate_refusal(request)

    renderings: dict[str, GroundedRendering] = {}
    grounding_by_rendering: dict[str, GroundingSet] = {}
    if reason is None:
        for grounding in request.grounding_sets:
            for rendering in grounding.renderings:
                if rendering.rendering_id in renderings:
                    reason = "two tool results reused one rendering identity."
                    break
                renderings[rendering.rendering_id] = rendering
                grounding_by_rendering[rendering.rendering_id] = grounding
            if reason is not None:
                break

    selected = candidate.rendering_ids
    if reason is None and not selected:
        reason = "the model selected no structured rendering."
    if reason is None and set(selected) - set(renderings):
        reason = "the model selected a claim absent from structured tool output."

    selected_groundings: tuple[GroundingSet, ...] = ()
    if reason is None:
        selected_ids = {grounding_by_rendering[item].grounding_id for item in selected}
        selected_groundings = tuple(
            grounding
            for grounding in request.grounding_sets
            if grounding.grounding_id in selected_ids
        )
        mandatory = {
            rendering_id
            for grounding in selected_groundings
            for rendering_id in grounding.mandatory_rendering_ids
        }
        if mandatory - set(selected):
            reason = "a mandatory semantic qualifier or factual context was omitted."

    missing_claim = _missing_claim_selection(selected_groundings, selected)
    if reason is None and missing_claim is not None:
        reason = f"the model selected no structured numerical claim from {missing_claim}."
    if reason is not None:
        return _refused(reason)

    parts = [renderings[rendering_id].text for rendering_id in selected]
    return AnswerPublicationDecision(released_text="\n".join(parts))
