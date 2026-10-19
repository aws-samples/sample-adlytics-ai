# SPDX-License-Identifier: MIT-0
"""Bounded typed custody of governed tool results used by the narrating agent."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from enum import StrEnum

from domain.conversation.contract_common import require_text
from domain.narrative.publication import GroundingSet, RenderingKind

__all__ = [
    "AgentToolCustodyV1",
    "HarnessStepDispositionV1",
    "HarnessToolStepV1",
    "ToolCustodyDispositionV1",
    "ValidatedToolResultV1",
    "validate_harness_trajectory",
]

_ALLOWED_TOOLS = frozenset(
    {
        "catalog_list_metrics",
        "catalog_describe_metric",
        "measurement_metric_by_grain",
        "measurement_compare_windows",
        "measurement_top_n",
    }
)


_RESULT_REF = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_MAX_STEPS = 24
_MAX_REF_LENGTH = 256


class ToolCustodyDispositionV1(StrEnum):
    """The retained outcome of one direct governed tool invocation."""

    SUCCEEDED = "succeeded"
    REFUSED = "refused"


class HarnessStepDispositionV1(StrEnum):
    """The outcome of one governed tool call in the order the Harness made it."""

    SUCCEEDED = "succeeded"
    REFUSED = "refused"
    ERRORED = "errored"


@dataclass(frozen=True, slots=True, kw_only=True)
class HarnessToolStepV1:
    """One governed tool call in invocation order.

    A step that returned a result carries the result reference the tool minted inside its
    response, which the tool also stamps on its own span, so an investigator reaches the call's
    own trace or log from the audit receipt (ADR-062); Gateway does not propagate trace context
    to Lambda targets. An errored call returned no result and has no reference.
    """

    ordinal: int
    tool_name: str
    disposition: HarnessStepDispositionV1
    step_ref: str | None

    def __post_init__(self) -> None:
        if self.ordinal < 1:
            raise ValueError("Harness step ordinal must be positive.")
        if not self.tool_name.strip():
            raise ValueError("Harness step requires a tool name.")
        if self.step_ref is not None and not (0 < len(self.step_ref) <= _MAX_REF_LENGTH):
            raise ValueError("Harness step reference is out of bounds.")
        if (self.disposition is HarnessStepDispositionV1.ERRORED) != (self.step_ref is None):
            raise ValueError("a Harness step has a result reference exactly when it did not error.")


def validate_harness_trajectory(steps: tuple[HarnessToolStepV1, ...]) -> None:
    """Require one bounded trajectory, contiguous and in invocation order."""
    if len(steps) > _MAX_STEPS:
        raise ValueError("Harness trajectory cannot exceed twenty-four steps.")
    if tuple(step.ordinal for step in steps) != tuple(range(1, len(steps) + 1)):
        raise ValueError("Harness trajectory must be contiguous and ordered.")


@dataclass(frozen=True, slots=True, kw_only=True)
class ValidatedToolResultV1:
    """A decoded result projection without raw provider payload or tool input."""

    tool_use_ref: str = field(repr=False)
    tool_name: str
    ordinal: int
    disposition: ToolCustodyDispositionV1
    tool_result_ref: str
    grounding_set: GroundingSet | None = field(default=None, repr=False)
    refusal_reason: str | None = field(default=None, repr=False)
    contract_version: str = "ShapedResponse.v1"

    def __post_init__(self) -> None:
        if not self.tool_use_ref.strip():
            raise ValueError("tool custody reference is required.")
        if not _RESULT_REF.fullmatch(self.tool_result_ref):
            raise ValueError("tool custody result reference must be the tool's correlation ID.")
        if self.tool_name not in _ALLOWED_TOOLS:
            raise ValueError("tool custody names an undeclared tool.")
        if self.ordinal < 1:
            raise ValueError("tool custody ordinal must be positive.")
        if not self.contract_version.strip():
            raise ValueError("tool custody contract version is required.")
        if self.disposition is ToolCustodyDispositionV1.SUCCEEDED:
            if self.grounding_set is None or self.refusal_reason is not None:
                raise ValueError(
                    "a successful tool custody result requires only a grounded projection."
                )
        elif self.refusal_reason is None or self.grounding_set is None:
            # A typed refusal is evidence: it keeps its reason and its grounded projection, which
            # evaluation judges the answer against (ADR-083).
            raise ValueError(
                "a refused tool custody result requires its refusal reason and its grounded "
                "refusal projection."
            )
        if self.refusal_reason is not None:
            require_text(self.refusal_reason, "tool refusal reason", maximum=4_000)


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentToolCustodyV1:
    """Current-turn decoded tool results retained for audit and evaluation, never prose gating."""

    results: tuple[ValidatedToolResultV1, ...] = field(repr=False)
    digest: str

    def __post_init__(self) -> None:
        refs = tuple(result.tool_use_ref for result in self.results)
        ordinals = tuple(result.ordinal for result in self.results)
        if len(set(refs)) != len(refs):
            raise ValueError("tool custody repeats a tool reference.")
        if ordinals != tuple(range(1, len(self.results) + 1)):
            raise ValueError("tool custody ordinals must be contiguous and ordered.")
        if self.digest != _digest(self.results):
            raise ValueError("tool custody digest does not match decoded results.")

    @classmethod
    def from_results(cls, results: tuple[ValidatedToolResultV1, ...]) -> AgentToolCustodyV1:
        """Create custody with a content-bound digest over decoded result projections."""
        return cls(results=results, digest=_digest(results))


def _digest(results: tuple[ValidatedToolResultV1, ...]) -> str:
    projection: list[object] = []
    for result in results:
        grounding = result.grounding_set
        projection.append(
            {
                "tool_use_ref": result.tool_use_ref,
                "tool_name": result.tool_name,
                "ordinal": result.ordinal,
                "tool_result_ref": result.tool_result_ref,
                "disposition": result.disposition.value,
                "refusal_reason": result.refusal_reason,
                "grounding": (
                    None
                    if grounding is None
                    else {
                        "grounding_id": grounding.grounding_id,
                        "mandatory_rendering_ids": grounding.mandatory_rendering_ids,
                        "requires_claim_selection": grounding.requires_claim_selection,
                        "renderings": tuple(
                            (
                                item.rendering_id,
                                item.text,
                                item.kind.value,
                                item.source_paths,
                                item.qualifier,
                            )
                            for item in grounding.renderings
                        ),
                        "collections": tuple(
                            (
                                item.collection_id,
                                item.heading,
                                item.items,
                                item.completeness.value,
                                item.source_paths,
                                item.required,
                                item.count_label,
                            )
                            for item in grounding.collections
                        ),
                    }
                ),
            }
        )
    canonical = json.dumps(projection, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def evaluation_grounding_sets(custody: AgentToolCustodyV1) -> tuple[GroundingSet, ...]:
    """Project custody into the grounding sets whose obligations evaluation judges the answer by.

    A refused call whose tool later succeeded in the same turn was an exploratory dead end
    the model already adapted around: its evidence stays in the context, but its reason stops
    being an obligation the final answer must carry (ADR-062: a refusal blocks only the
    claims that depended on it). A refusal never superseded remains fully mandatory.
    """
    succeeded_after: dict[str, int] = {}
    for result in custody.results:
        if result.disposition is ToolCustodyDispositionV1.SUCCEEDED:
            succeeded_after[result.tool_name] = result.ordinal
    projected: list[GroundingSet] = []
    for result in custody.results:
        grounding = result.grounding_set
        if grounding is None:
            continue
        superseded = (
            result.disposition is ToolCustodyDispositionV1.REFUSED
            and succeeded_after.get(result.tool_name, 0) > result.ordinal
        )
        if superseded and not any(
            rendering.kind is RenderingKind.QUALIFIER for rendering in grounding.renderings
        ):
            projected.append(replace(grounding, mandatory_rendering_ids=()))
        else:
            projected.append(grounding)
    return tuple(projected)
