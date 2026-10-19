# SPDX-License-Identifier: MIT-0
"""What tool custody and the governed trajectory retain, so audit and evaluation can use them."""

import pytest
from governed_evidence_fixture import FIXTURE_TOOL_RESULT_REF, shaped_response

from domain.conversation.tool_custody import (
    AgentToolCustodyV1,
    HarnessStepDispositionV1,
    HarnessToolStepV1,
    ToolCustodyDispositionV1,
    ValidatedToolResultV1,
    evaluation_grounding_sets,
    validate_harness_trajectory,
)
from domain.narrative.publication import (
    GroundedRendering,
    GroundingSet,
    QualifierKind,
    RenderingKind,
    grounded_refusal,
)

pytestmark = pytest.mark.unit

_REASON = "The requested metric is declared, but the facts behind it are not built yet."
_RESULT_REF = "7a1c9f2e-3b4d-4e5f-8a6b-1c2d3e4f5a6b"


def _refusal_grounding() -> GroundingSet:
    return grounded_refusal(_REASON, blocks_answer=True, identity_salt="corr-1")


@pytest.mark.covers("ADR-062")
def test_a_refused_result_retains_its_reason_and_its_grounded_refusal() -> None:
    """The deployed failure began here: custody forbade grounding on refused results."""
    result = ValidatedToolResultV1(
        tool_use_ref="use-1",
        tool_name="catalog_describe_metric",
        ordinal=1,
        disposition=ToolCustodyDispositionV1.REFUSED,
        tool_result_ref=_RESULT_REF,
        grounding_set=_refusal_grounding(),
        refusal_reason=_REASON,
    )

    assert result.grounding_set is not None
    assert result.grounding_set.renderings[0].kind is RenderingKind.REFUSAL


def test_a_refused_result_without_grounding_cannot_be_constructed() -> None:
    """Evaluation judges the answer against the refusal, so custody must keep its projection."""
    with pytest.raises(ValueError, match="grounded refusal projection"):
        ValidatedToolResultV1(
            tool_use_ref="use-1",
            tool_name="catalog_describe_metric",
            ordinal=1,
            disposition=ToolCustodyDispositionV1.REFUSED,
            tool_result_ref=_RESULT_REF,
            refusal_reason=_REASON,
        )


def test_a_refused_result_without_a_reason_cannot_be_constructed() -> None:
    with pytest.raises(ValueError, match="refusal reason"):
        ValidatedToolResultV1(
            tool_use_ref="use-1",
            tool_name="catalog_describe_metric",
            ordinal=1,
            disposition=ToolCustodyDispositionV1.REFUSED,
            tool_result_ref=_RESULT_REF,
            grounding_set=_refusal_grounding(),
        )


def test_a_successful_result_cannot_carry_a_refusal_reason() -> None:
    with pytest.raises(ValueError, match="only a grounded projection"):
        ValidatedToolResultV1(
            tool_use_ref="use-1",
            tool_name="catalog_describe_metric",
            ordinal=1,
            disposition=ToolCustodyDispositionV1.SUCCEEDED,
            tool_result_ref=_RESULT_REF,
            grounding_set=_refusal_grounding(),
            refusal_reason=_REASON,
        )


@pytest.mark.covers("ADR-062")
def test_a_result_reference_must_be_the_tool_minted_correlation_id() -> None:
    """Only the correlation ID the tool stamped on its own span reaches that call's trace."""
    with pytest.raises(ValueError, match="correlation ID"):
        ValidatedToolResultV1(
            tool_use_ref="use-1",
            tool_name="catalog_describe_metric",
            ordinal=1,
            disposition=ToolCustodyDispositionV1.REFUSED,
            tool_result_ref="mcp-message-1",
            grounding_set=_refusal_grounding(),
            refusal_reason=_REASON,
        )


@pytest.mark.covers("ADR-083")
def test_the_retired_finalizer_is_not_a_declared_custody_tool() -> None:
    with pytest.raises(ValueError, match="undeclared tool"):
        ValidatedToolResultV1(
            tool_use_ref="use-1",
            tool_name="presentation_publish",
            ordinal=1,
            disposition=ToolCustodyDispositionV1.SUCCEEDED,
            tool_result_ref=_RESULT_REF,
            grounding_set=shaped_response().metadata.grounding,
        )


def _step(ordinal: int, disposition: HarnessStepDispositionV1) -> HarnessToolStepV1:
    errored = disposition is HarnessStepDispositionV1.ERRORED
    return HarnessToolStepV1(
        ordinal=ordinal,
        tool_name="catalog_describe_metric",
        disposition=disposition,
        step_ref=None if errored else _RESULT_REF,
    )


@pytest.mark.covers("ADR-083", "ADR-062")
def test_a_trajectory_is_every_governed_call_in_invocation_order() -> None:
    steps = (
        _step(1, HarnessStepDispositionV1.ERRORED),
        _step(2, HarnessStepDispositionV1.REFUSED),
        _step(3, HarnessStepDispositionV1.SUCCEEDED),
    )

    validate_harness_trajectory(steps)

    assert [step.disposition.value for step in steps] == ["errored", "refused", "succeeded"]


@pytest.mark.covers("ADR-083")
def test_a_turn_without_tool_calls_has_an_empty_valid_trajectory() -> None:
    """Nothing now closes a trajectory: an answer written from no tool call is a valid turn."""
    validate_harness_trajectory(())


@pytest.mark.parametrize("ordinals", [(1, 3), (2,), (2, 1), (1, 1)])
def test_a_trajectory_that_is_not_contiguous_and_ordered_is_rejected(
    ordinals: tuple[int, ...],
) -> None:
    steps = tuple(_step(ordinal, HarnessStepDispositionV1.SUCCEEDED) for ordinal in ordinals)

    with pytest.raises(ValueError, match="contiguous"):
        validate_harness_trajectory(steps)


def test_a_trajectory_is_bounded_at_twenty_four_steps() -> None:
    steps = tuple(_step(ordinal, HarnessStepDispositionV1.SUCCEEDED) for ordinal in range(1, 26))

    validate_harness_trajectory(steps[:24])
    with pytest.raises(ValueError, match="twenty-four"):
        validate_harness_trajectory(steps)


@pytest.mark.covers("ADR-083")
@pytest.mark.parametrize(
    ("disposition", "step_ref"),
    [
        (HarnessStepDispositionV1.SUCCEEDED, None),
        (HarnessStepDispositionV1.REFUSED, None),
        (HarnessStepDispositionV1.ERRORED, _RESULT_REF),
    ],
)
def test_a_step_has_a_result_reference_exactly_when_it_did_not_error(
    disposition: HarnessStepDispositionV1, step_ref: str | None
) -> None:
    with pytest.raises(ValueError, match="exactly when it did not error"):
        HarnessToolStepV1(
            ordinal=1,
            tool_name="catalog_describe_metric",
            disposition=disposition,
            step_ref=step_ref,
        )


@pytest.mark.parametrize(
    ("step", "message"),
    [
        ((0, "catalog_describe_metric", _RESULT_REF), "ordinal must be positive"),
        ((1, " ", _RESULT_REF), "requires a tool name"),
        ((1, "catalog_describe_metric", ""), "out of bounds"),
        ((1, "catalog_describe_metric", "r" * 257), "out of bounds"),
    ],
)
def test_a_step_is_bounded(step: tuple[int, str, str], message: str) -> None:
    ordinal, tool_name, step_ref = step
    with pytest.raises(ValueError, match=message):
        HarnessToolStepV1(
            ordinal=ordinal,
            tool_name=tool_name,
            disposition=HarnessStepDispositionV1.SUCCEEDED,
            step_ref=step_ref,
        )


def test_a_step_reference_at_its_length_bound_is_accepted() -> None:
    step = HarnessToolStepV1(
        ordinal=1,
        tool_name="catalog_describe_metric",
        disposition=HarnessStepDispositionV1.REFUSED,
        step_ref="r" * 256,
    )

    assert step.step_ref == "r" * 256


def _refused(ordinal: int, tool_name: str, grounding: GroundingSet) -> ValidatedToolResultV1:
    return ValidatedToolResultV1(
        tool_use_ref=f"use-{ordinal}",
        tool_name=tool_name,
        ordinal=ordinal,
        disposition=ToolCustodyDispositionV1.REFUSED,
        tool_result_ref=f"{ordinal}{ordinal}{ordinal}{ordinal}1111-1111-4111-8111-111111111111",
        grounding_set=grounding,
        refusal_reason=grounding.renderings[0].text,
    )


def _succeeded(ordinal: int, tool_name: str) -> ValidatedToolResultV1:
    return ValidatedToolResultV1(
        tool_use_ref=f"use-{ordinal}",
        tool_name=tool_name,
        ordinal=ordinal,
        disposition=ToolCustodyDispositionV1.SUCCEEDED,
        tool_result_ref=FIXTURE_TOOL_RESULT_REF,
        grounding_set=shaped_response().metadata.grounding,
    )


@pytest.mark.covers("ADR-062", "ADR-083")
def test_a_refusal_superseded_by_a_later_success_stops_being_an_obligation() -> None:
    """An exploratory dead end the model adapted around must not raise the answer's bar.

    Observed live: every refused attempt added a mandatory rendering, so the healthier the
    exploration, the harder the answer's obligations became.
    """
    grounding = shaped_response().metadata.grounding
    assert grounding is not None
    refusal = grounded_refusal(
        "A fictional grain constraint refused the first attempt.",
        blocks_answer=False,
        identity_salt="superseded-refusal",
    )
    unresolved_refusal = grounded_refusal(
        "A fictional metric that is declared but not built.",
        blocks_answer=True,
        identity_salt="standing-refusal",
    )
    value = AgentToolCustodyV1.from_results(
        (
            _refused(1, "measurement_metric_by_grain", refusal),
            _succeeded(2, "measurement_metric_by_grain"),
            _refused(3, "catalog_describe_metric", unresolved_refusal),
        )
    )

    projected = evaluation_grounding_sets(value)

    by_id = {item.grounding_id: item for item in projected}
    assert by_id[refusal.grounding_id].mandatory_rendering_ids == ()
    assert by_id[refusal.grounding_id].renderings == refusal.renderings
    assert (
        by_id[unresolved_refusal.grounding_id].mandatory_rendering_ids
        == unresolved_refusal.mandatory_rendering_ids
    )
    assert by_id[grounding.grounding_id] == grounding
    assert [item.grounding_id for item in projected] == [
        refusal.grounding_id,
        grounding.grounding_id,
        unresolved_refusal.grounding_id,
    ]


def test_a_refusal_after_the_last_success_of_its_tool_stays_mandatory() -> None:
    refusal = grounded_refusal(
        "A fictional later attempt was refused.", blocks_answer=False, identity_salt="later"
    )
    value = AgentToolCustodyV1.from_results(
        (_succeeded(1, "measurement_top_n"), _refused(2, "measurement_top_n", refusal))
    )

    projected = evaluation_grounding_sets(value)

    assert projected[1] == refusal


def test_a_superseded_refusal_carrying_a_qualifier_keeps_its_obligations() -> None:
    """A qualifier is mandatory at release by construction, so supersession cannot drop it."""
    qualified = GroundingSet(
        grounding_id="grounding-fictional-qualified-refusal",
        renderings=(
            GroundedRendering(
                rendering_id="grounding-fictional-qualified-refusal:reason",
                text="A fictional grain was refused.",
                kind=RenderingKind.CONTEXT,
                source_paths=("refusal.reason",),
            ),
            GroundedRendering(
                rendering_id="grounding-fictional-qualified-refusal:stale",
                text="Fictional data was last refreshed a week ago.",
                kind=RenderingKind.QUALIFIER,
                source_paths=("metadata.freshness_status",),
                qualifier=QualifierKind.STALE,
            ),
        ),
        mandatory_rendering_ids=(
            "grounding-fictional-qualified-refusal:reason",
            "grounding-fictional-qualified-refusal:stale",
        ),
        requires_claim_selection=False,
    )
    value = AgentToolCustodyV1.from_results(
        (_refused(1, "measurement_top_n", qualified), _succeeded(2, "measurement_top_n"))
    )

    projected = evaluation_grounding_sets(value)

    assert projected[0] == qualified
