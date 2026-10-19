# SPDX-License-Identifier: MIT-0
"""Grounded numerical claim selection and qualifier-preserving answer publication."""

import pytest

from domain.narrative.publication import (
    AnswerPublicationRequest,
    CandidateNarration,
    GroundedRendering,
    GroundingSet,
    QualifierKind,
    RenderingKind,
    grounded_refusal,
    validate_answer_publication,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-049")]


def _grounding(*qualifiers: QualifierKind, identity: str = "g") -> GroundingSet:
    qualifier_renderings = tuple(
        GroundedRendering(
            rendering_id=f"{identity}:{kind.value}",
            text=f"Mandatory {kind.value} caveat.",
            kind=RenderingKind.QUALIFIER,
            source_paths=(f"metadata.qualifiers.{kind.value}",),
            qualifier=kind,
        )
        for kind in qualifiers
    )
    return GroundingSet(
        grounding_id=identity,
        renderings=(
            GroundedRendering(
                rendering_id=f"{identity}:claim",
                text="revenue=88.5000; units=USD currency; grain=day.",
                kind=RenderingKind.CLAIM,
                source_paths=("data.values[0]", "data.units", "data.grain"),
            ),
            *qualifier_renderings,
        ),
        mandatory_rendering_ids=tuple(item.rendering_id for item in qualifier_renderings),
        requires_claim_selection=True,
    )


def _decision(*ids: str, connective: str = ""):
    return validate_answer_publication(
        AnswerPublicationRequest(
            candidate=CandidateNarration(connective, ids),
            grounding_sets=(_grounding(),),
        )
    )


class TestNumericalClaims:
    def test_empty_structured_selection_is_refused(self) -> None:
        decision = _decision()

        assert not decision.released
        assert decision.refusal_reason is not None
        assert "no structured rendering" in decision.refusal_reason

    def test_exact_domain_rendering_is_released_without_reformatting(self) -> None:
        decision = _decision("g:claim")

        assert decision.released
        assert decision.released_text == "revenue=88.5000; units=USD currency; grain=day."

    def test_even_number_free_model_prose_is_not_released(self) -> None:
        decision = _decision("g:claim", connective="Here is the result.")

        assert not decision.released
        assert decision.released_text is None

    @pytest.mark.parametrize(
        "candidate",
        [
            CandidateNarration("A new figure is 99.", ("g:claim",)),
            CandidateNarration("The result is below.", ("g:changed",)),
            CandidateNarration("I summed it to 177.0000.", ("g:claim",)),
            CandidateNarration("That is about one hundred.", ("g:claim",)),
            CandidateNarration("The increase is ten percent.", ("g:claim",)),
        ],
    )
    def test_added_changed_recomputed_or_spelled_out_number_is_withheld(
        self,
        candidate: CandidateNarration,
    ) -> None:
        decision = validate_answer_publication(AnswerPublicationRequest(candidate, (_grounding(),)))

        assert not decision.released
        assert decision.released_text is None

    def test_claim_must_keep_value_units_and_grain_as_one_rendering(self) -> None:
        decision = _decision("g:claim")

        assert decision.released_text is not None
        assert "88.5000" in decision.released_text
        assert "USD currency" in decision.released_text
        assert "grain=day" in decision.released_text

    def test_response_with_figures_cannot_select_context_only(self) -> None:
        grounding = GroundingSet(
            grounding_id="g",
            renderings=(
                GroundedRendering(
                    "g:context",
                    "The query completed.",
                    RenderingKind.CONTEXT,
                    ("narrative[0]",),
                ),
            ),
            mandatory_rendering_ids=("g:context",),
            requires_claim_selection=True,
        )
        decision = validate_answer_publication(
            AnswerPublicationRequest(
                CandidateNarration("", ("g:context",)),
                (grounding,),
            )
        )

        assert not decision.released


class TestMandatoryQualifiers:
    @pytest.mark.parametrize("kind", list(QualifierKind))
    def test_each_qualifier_is_independently_required(self, kind: QualifierKind) -> None:
        grounding = _grounding(kind)
        omitted = validate_answer_publication(
            AnswerPublicationRequest(
                CandidateNarration("", ("g:claim",)),
                (grounding,),
            )
        )
        included = validate_answer_publication(
            AnswerPublicationRequest(
                CandidateNarration("", ("g:claim", f"g:{kind.value}")),
                (grounding,),
            )
        )

        assert not omitted.released
        assert included.released
        assert included.released_text is not None
        assert f"Mandatory {kind.value} caveat." in included.released_text

    def test_mandatory_factual_context_cannot_be_omitted(self) -> None:
        context = GroundedRendering(
            rendering_id="g:metric-default",
            text="Metric selection used the governed default.",
            kind=RenderingKind.CONTEXT,
            source_paths=("metadata.resolution_provenance[0].origin",),
        )
        base = _grounding()
        grounding = GroundingSet(
            grounding_id=base.grounding_id,
            renderings=(*base.renderings, context),
            mandatory_rendering_ids=(context.rendering_id,),
            requires_claim_selection=True,
        )
        omitted = validate_answer_publication(
            AnswerPublicationRequest(
                CandidateNarration("", ("g:claim",)),
                (grounding,),
            )
        )
        included = validate_answer_publication(
            AnswerPublicationRequest(
                CandidateNarration("", ("g:claim", context.rendering_id)),
                (grounding,),
            )
        )

        assert not omitted.released
        assert included.released_text == (
            "revenue=88.5000; units=USD currency; grain=day.\n"
            "Metric selection used the governed default."
        )

    def test_unknown_rendering_cannot_impersonate_a_qualifier(self) -> None:
        grounding = _grounding(QualifierKind.SUPPRESSED)
        decision = validate_answer_publication(
            AnswerPublicationRequest(
                CandidateNarration("", ("g:claim", "g:suppressed-copy")),
                (grounding,),
            )
        )

        assert not decision.released


class TestGroundedRefusal:
    def test_exact_safe_refusal_can_be_selected_without_becoming_model_prose(self) -> None:
        grounding = grounded_refusal("The operation is not supported.")
        rendering_id = grounding.renderings[0].rendering_id

        decision = validate_answer_publication(
            AnswerPublicationRequest(CandidateNarration("", (rendering_id,)), (grounding,))
        )

        assert decision.released_text == "The operation is not supported."
        assert grounding.mandatory_rendering_ids == (rendering_id,)
        assert grounding.requires_claim_selection is False

    def test_blocking_refusal_prevents_a_later_weakened_success_from_releasing(self) -> None:
        refusal = grounded_refusal(
            "The operation candidate is not supported by this measurement.",
            blocks_answer=True,
        )
        success = _grounding(identity="success")

        decision = validate_answer_publication(
            AnswerPublicationRequest(
                CandidateNarration("", ("success:claim",)),
                (refusal, success),
            )
        )

        assert decision.released_text == (
            "The operation candidate is not supported by this measurement."
        )
        assert refusal.renderings[0].kind is RenderingKind.REFUSAL


class TestGroundingIdentity:
    def test_two_tool_results_cannot_reuse_a_grounding_identity(self) -> None:
        with pytest.raises(ValueError, match="repeats a grounding set"):
            AnswerPublicationRequest(
                CandidateNarration("", ("g:claim",)),
                (_grounding(), _grounding()),
            )

    def test_unselected_exploratory_result_does_not_block_selected_final_claim(self) -> None:
        first = _grounding(QualifierKind.SUPPRESSED, identity="first")
        second = _grounding(QualifierKind.SUPPRESSED, identity="second")
        decision = validate_answer_publication(
            AnswerPublicationRequest(
                CandidateNarration("", ("first:claim", "first:suppressed")),
                (first, second),
            )
        )

        assert decision.released
        assert decision.released_text == (
            "revenue=88.5000; units=USD currency; grain=day.\nMandatory suppressed caveat."
        )

    def test_selecting_a_set_activates_only_that_sets_mandatory_renderings(self) -> None:
        first = _grounding(QualifierKind.SUPPRESSED, identity="first")
        second = _grounding(QualifierKind.SUPPRESSED, identity="second")
        omitted_selected_qualifier = validate_answer_publication(
            AnswerPublicationRequest(
                CandidateNarration("", ("first:claim",)),
                (first, second),
            )
        )

        assert not omitted_selected_qualifier.released
        assert omitted_selected_qualifier.refusal_reason is not None
        assert "mandatory" in omitted_selected_qualifier.refusal_reason

    def test_missing_grounding_fails_closed(self) -> None:
        decision = validate_answer_publication(
            AnswerPublicationRequest(CandidateNarration("No figure.", ()), ())
        )

        assert not decision.released
