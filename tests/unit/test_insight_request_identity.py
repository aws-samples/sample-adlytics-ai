# SPDX-License-Identifier: MIT-0
"""Server-owned analytical-scope identity for immutable insight cache requests."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import cast

import pytest

from adapters.inbound.presentation_handlers.insights import InsightAssemblyRequest
from adapters.insight_requests import (
    DeclaredInsightRequestBatch,
    DeclaredInsightRequestInput,
    InsightRequestContext,
    analytical_scope_id,
    declared_insight_requests,
)
from domain.presentation.insights import (
    BasisThresholds,
    DeterministicFinding,
    FindingUnit,
    HorizonThresholds,
    InsightCalibrationCase,
    InsightCalibrationDraft,
    InsightCalibrationKind,
    InsightContextEntry,
    InsightContract,
    InsightDeepContext,
    InsightEvidenceBundle,
    InsightEvidenceInput,
    InsightFindingKind,
    InsightGenerationRequest,
    InsightHorizonRole,
    InsightMateriality,
    InsightModelRoute,
    InsightOutputClass,
    InsightPromptActivation,
    InsightSlotId,
    InsightSlotPredicate,
    InsightStructuredOutputSchema,
    InsightWindowProfile,
    MetricScale,
    NearZeroPriors,
    OperandBasis,
    PredicateFinding,
    insight_input_digest,
    insight_input_document,
)
from domain.presentation.temporal import TemporalDisplayGrain
from domain.semantics.analytical_resolution import AuthorizedAnalyticalScope
from domain.semantics.identity import CommerceScopeKey
from domain.shared_kernel.values import DateRange

pytestmark = pytest.mark.unit

_PROFILE = InsightWindowProfile(
    days=7,
    grain=TemporalDisplayGrain.DAY,
    prior_start="2026-07-25",
    prior_end="2026-07-31",
    role=InsightHorizonRole.TACTICAL_REVIEW,
)


def _scope(*values: str) -> AuthorizedAnalyticalScope:
    return AuthorizedAnalyticalScope(frozenset(CommerceScopeKey(value) for value in values))


def _contract() -> InsightContract:
    return InsightContract(
        id=InsightSlotId.CONTRIBUTION,
        version=1,
        purpose="Explain one server-computed contribution.",
        display_name="Contribution (fictional)",
        output_class=InsightOutputClass.PERSPECTIVE,
        allowed_inputs=("fictional deterministic finding",),
        deterministic_findings=(InsightFindingKind.ABSOLUTE_CHANGE,),
        predicate=InsightSlotPredicate(
            (InsightEvidenceInput.CAMPAIGN_COMPARISONS,),
            (InsightFindingKind.ABSOLUTE_CHANGE,),
            InsightMateriality(
                {
                    OperandBasis.CAMPAIGN_WINDOW: BasisThresholds(
                        {
                            FindingUnit.CURRENCY: HorizonThresholds(
                                Decimal(1), Decimal(1), Decimal(1)
                            )
                        }
                    )
                },
                NearZeroPriors({}),
            ),
            (),
        ),
        silence_condition="No deterministic finding is available.",
        structured_output_schema=InsightStructuredOutputSchema(
            "insight-narration-v1", ("output_class", "sentences")
        ),
        initial_fictional_prompt="Use only supplied findings.",
        calibration_cases=tuple(
            InsightCalibrationCase(
                kind,
                "Fictional input.",
                "Expected outcome.",
                (DeterministicFinding("Fictional finding", Decimal("1.5")),),
                draft=None
                if kind
                in {InsightCalibrationKind.KNOWN_CORRECT, InsightCalibrationKind.EXPLORATORY}
                else InsightCalibrationDraft(("A fictional draft sentence.",)),
            )
            for kind in InsightCalibrationKind
        ),
        deep=InsightDeepContext(
            ("fictional full-context input",),
            InsightStructuredOutputSchema(
                "insight-deep-dive-v1",
                (
                    "what_stands_out",
                    "candidate_readings",
                    "discriminating_evidence",
                    "cited_figures",
                ),
            ),
        ),
    )


def _request(scope: AuthorizedAnalyticalScope) -> object:
    return declared_insight_requests(
        DeclaredInsightRequestInput(
            context=InsightRequestContext(
                analytical_scope_id(scope),
                "campaign-fictional",
                DateRange(date(2026, 8, 1), date(2026, 8, 7)),
                "sha256:artifact-fictional",
            ),
            contracts={InsightSlotId.CONTRIBUTION: _contract()},
            findings_by_slot={
                InsightSlotId.CONTRIBUTION: (
                    DeterministicFinding("Fictional Campaign", Decimal("5")),
                )
            },
            slot_ids=(InsightSlotId.CONTRIBUTION,),
            window_profile=_PROFILE,
            evidence_by_slot={
                InsightSlotId.CONTRIBUTION: InsightEvidenceBundle(
                    (InsightEvidenceInput.CAMPAIGN_COMPARISONS,),
                    (
                        PredicateFinding(
                            DeterministicFinding("Fictional Campaign", Decimal("5")),
                            InsightFindingKind.ABSOLUTE_CHANGE,
                            (InsightEvidenceInput.CAMPAIGN_COMPARISONS,),
                            FindingUnit.CURRENCY,
                            OperandBasis.CAMPAIGN_WINDOW,
                            "ad_spend",
                        ),
                    ),
                )
            },
        )
    ).requests[0]


@pytest.mark.covers("ADR-068")
def test_users_in_one_authorized_analytical_scope_share_one_cache_key() -> None:
    first = _request(_scope("commerce-b", "commerce-a"))
    second = _request(_scope("commerce-a", "commerce-b"))

    assert first.key == second.key  # type: ignore[attr-defined]
    assert first.key.ownership_key == (  # type: ignore[attr-defined]
        "SCOPE#analytical-scope-v1:commerce-a,commerce-b#SUBJECT#campaign-fictional"
    )


@pytest.mark.covers("ADR-068")
def test_distinct_authorized_analytical_scopes_never_share_a_cache_key() -> None:
    first = _request(_scope("commerce-a"))
    second = _request(_scope("commerce-b"))

    assert first.key != second.key  # type: ignore[attr-defined]
    assert first.key.ownership_key != second.key.ownership_key  # type: ignore[attr-defined]


@pytest.mark.covers("ADR-068")
def test_predicate_admission_requires_declared_evidence_kind_and_materiality() -> None:
    context = InsightRequestContext(
        "analytical-scope-v1:commerce-fictional",
        "campaign-fictional",
        DateRange(date(2026, 8, 1), date(2026, 8, 7)),
        "sha256:artifact-fictional",
    )
    contract = _contract()

    def requests(evidence: InsightEvidenceBundle) -> DeclaredInsightRequestBatch:
        return declared_insight_requests(
            DeclaredInsightRequestInput(
                context=context,
                contracts={InsightSlotId.CONTRIBUTION: contract},
                findings_by_slot={},
                slot_ids=(InsightSlotId.CONTRIBUTION,),
                window_profile=_PROFILE,
                evidence_by_slot={InsightSlotId.CONTRIBUTION: evidence},
            )
        )

    accepted = requests(
        InsightEvidenceBundle(
            (InsightEvidenceInput.CAMPAIGN_COMPARISONS,),
            (
                PredicateFinding(
                    DeterministicFinding("Fictional Campaign", Decimal("5")),
                    InsightFindingKind.ABSOLUTE_CHANGE,
                    (InsightEvidenceInput.CAMPAIGN_COMPARISONS,),
                    FindingUnit.CURRENCY,
                    OperandBasis.CAMPAIGN_WINDOW,
                    "ad_spend",
                ),
            ),
        )
    )
    missing_evidence = requests(
        InsightEvidenceBundle(
            (),
            (
                PredicateFinding(
                    DeterministicFinding("Fictional Campaign", Decimal("5")),
                    InsightFindingKind.ABSOLUTE_CHANGE,
                    (InsightEvidenceInput.CAMPAIGN_COMPARISONS,),
                    FindingUnit.CURRENCY,
                    OperandBasis.CAMPAIGN_WINDOW,
                    "ad_spend",
                ),
            ),
        )
    )
    missing_kind = requests(
        InsightEvidenceBundle(
            (InsightEvidenceInput.CAMPAIGN_COMPARISONS,),
            (
                PredicateFinding(
                    DeterministicFinding("Fictional Campaign", Decimal("5")),
                    InsightFindingKind.PERCENTAGE_CHANGE,
                    (InsightEvidenceInput.CAMPAIGN_COMPARISONS,),
                    FindingUnit.PERCENT,
                    OperandBasis.CAMPAIGN_WINDOW,
                    "ad_spend",
                    MetricScale.CURRENCY,
                ),
            ),
        )
    )
    immaterial = requests(
        InsightEvidenceBundle(
            (InsightEvidenceInput.CAMPAIGN_COMPARISONS,),
            (
                PredicateFinding(
                    DeterministicFinding("Fictional Campaign", Decimal("0.5")),
                    InsightFindingKind.ABSOLUTE_CHANGE,
                    (InsightEvidenceInput.CAMPAIGN_COMPARISONS,),
                    FindingUnit.CURRENCY,
                    OperandBasis.CAMPAIGN_WINDOW,
                    "ad_spend",
                ),
            ),
        )
    )

    assert len(accepted.requests) == 1
    # An admitted request carries the model-silence reason, never the contract statement.
    assert accepted.requests[0].silence_reason == (
        "Nothing here stands out beyond what the chart already shows."
    )
    assert not accepted.silence_reasons and not accepted.missing_evidence_by_slot
    # Absent required evidence is a distinct declared outcome naming the missing inputs.
    assert missing_evidence.requests == ()
    assert missing_evidence.missing_evidence_by_slot == {
        InsightSlotId.CONTRIBUTION: (InsightEvidenceInput.CAMPAIGN_COMPARISONS,)
    }
    assert not missing_evidence.silence_reasons
    # Complete evidence whose predicate admits nothing is evaluated-quiet, and the served
    # customer reason names why (W13), never the contract's silence condition.
    assert missing_kind.requests == ()
    assert missing_kind.silence_reasons == {
        InsightSlotId.CONTRIBUTION: "This view doesn't have the comparison this insight needs."
    }
    assert not missing_kind.missing_evidence_by_slot
    assert immaterial.requests == ()
    assert immaterial.silence_reasons == {
        InsightSlotId.CONTRIBUTION: "No change in this view is large enough to call out."
    }
    assert not immaterial.missing_evidence_by_slot


def _assembly() -> InsightAssemblyRequest:
    return cast("InsightAssemblyRequest", _request(_scope("commerce-a")))


def _generation(assembly: InsightAssemblyRequest) -> InsightGenerationRequest:
    key = replace(assembly.key, prompt_version="v1")
    return InsightGenerationRequest(
        key,
        assembly.output_class,
        InsightPromptActivation(key.slot_id, "v1", "A fictional prompt.", InsightModelRoute.QUICK),
        assembly.purpose,
        assembly.findings,
        assembly.window_profile,
        assembly.context,
    )


@pytest.mark.covers("ADR-076")
def test_the_card_key_carries_the_digest_of_its_own_model_input() -> None:
    assembly = _assembly()

    assert assembly.key.input_digest == insight_input_digest(insight_input_document(assembly))
    assert assembly.key.content_key.endswith(f"#INPUT#{assembly.key.input_digest}")
    # Positive control: the request the serve path builds from it is accepted.
    assert _generation(assembly).key.input_digest == assembly.key.input_digest


@pytest.mark.covers("ADR-076")
def test_one_changed_context_value_is_a_new_card_identity() -> None:
    assembly = _assembly()
    with_context = replace(
        assembly, context=(InsightContextEntry("reporting window", "2026-08-01 to 2026-08-07"),)
    )
    edited = replace(
        with_context,
        context=(InsightContextEntry("reporting window", "2026-08-02 to 2026-08-07"),),
    )

    first = insight_input_digest(insight_input_document(with_context))
    second = insight_input_digest(insight_input_document(edited))

    assert first != second
    assert (
        replace(assembly.key, input_digest=first).content_key
        != replace(assembly.key, input_digest=second).content_key
    )


@pytest.mark.covers("ADR-076")
def test_a_card_generation_with_another_inputs_digest_is_refused() -> None:
    assembly = _assembly()
    stale = replace(
        assembly,
        findings=(DeterministicFinding("Fictional Campaign", Decimal("6")),),
    )

    with pytest.raises(ValueError, match="must match its cache input digest"):
        _generation(stale)
