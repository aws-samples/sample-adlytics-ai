# SPDX-License-Identifier: MIT-0
"""Executable evidence predicates for campaign insight request admission."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from functools import partial
from pathlib import Path

import pytest
from campaign_detail_support import detail_fixture

from adapters.campaign_insight_requests import (
    DetailInsightRequestDerivationInput,
    detail_insight_request_derivation,
)
from adapters.jobs.contract_validation.insight_loader import load_insight_contracts
from domain.presentation.campaign_attribution import (
    CampaignDelivery,
    CampaignDeliverySlot,
    CampaignDeliveryValue,
    campaign_delivery_missing_statuses,
)
from domain.presentation.insights import (
    MATERIALITY_UNDECLARED,
    BasisThresholds,
    DeliveryFollowUpEvidence,
    DeterministicFinding,
    FindingUnit,
    HorizonThresholds,
    InsightCalibrationCase,
    InsightCalibrationDraft,
    InsightCalibrationKind,
    InsightContract,
    InsightDeepContext,
    InsightEvidenceBundle,
    InsightEvidenceInput,
    InsightFindingKind,
    InsightHorizonRole,
    InsightMateriality,
    InsightOutputClass,
    InsightSlotId,
    InsightSlotPredicate,
    InsightStructuredOutputSchema,
    MetricScale,
    NearZeroPriors,
    OperandBasis,
    PredicateFinding,
    admitted_predicate_findings,
    finding_is_material,
    horizon_role_for,
)
from domain.semantics.analytical_resolution import AuthorizedAnalyticalScope
from domain.semantics.identity import CommerceScopeKey
from domain.shared_kernel.errors import InvariantError

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def _contracts() -> dict[InsightSlotId, object]:
    return {contract.id: contract for _, contract in load_insight_contracts(ROOT)}


_CAMPAIGN = OperandBasis.CAMPAIGN_WINDOW
_INPUTS = (InsightEvidenceInput.CAMPAIGN_COMPARISONS,)


def _finding(  # noqa: PLR0913 — a test builder; every coordinate is by keyword
    name: str,
    value: str,
    kind: InsightFindingKind = InsightFindingKind.ABSOLUTE_CHANGE,
    *,
    unit: FindingUnit = FindingUnit.CURRENCY,
    basis: OperandBasis = _CAMPAIGN,
    metric: str = "ad_spend",
    inputs: tuple[InsightEvidenceInput, ...] = _INPUTS,
    prior: str | None = None,
) -> PredicateFinding:
    """Build one finding; a percentage carries its metric's scale, as builders stamp it."""
    scale = (
        {
            FindingUnit.CURRENCY: MetricScale.CURRENCY,
            FindingUnit.MULTIPLIER: MetricScale.MULTIPLIER,
            FindingUnit.RATE_PP: MetricScale.RATE_PP,
            FindingUnit.UNIT_PRICE: MetricScale.UNIT_PRICE,
            FindingUnit.COUNT: MetricScale.COUNT,
        }[unit]
        if kind is InsightFindingKind.PERCENTAGE_CHANGE
        else None
    )
    return PredicateFinding(
        DeterministicFinding(name, Decimal(value)),
        kind,
        inputs,
        FindingUnit.PERCENT if kind is InsightFindingKind.PERCENTAGE_CHANGE else unit,
        basis,
        metric,
        scale,
        prior_magnitude=None if prior is None else Decimal(prior),
    )


@pytest.mark.covers("ADR-068")
def test_divergence_requires_portfolio_direction_and_returns_only_material_evidence() -> None:
    contract = _contracts()[InsightSlotId.DIVERGENCE]
    portfolio = partial(
        _finding,
        unit=FindingUnit.MULTIPLIER,
        basis=OperandBasis.PORTFOLIO_WINDOW,
        metric="portfolio_roas_attributed_14d",
        inputs=(InsightEvidenceInput.PORTFOLIO_DIRECTION,),
    )
    campaign = partial(_finding, unit=FindingUnit.MULTIPLIER, metric="roas_attributed_14d")
    percent = InsightFindingKind.PERCENTAGE_CHANGE
    # Thresholds (tactical-review): portfolio multiplier 0.15 at portfolio_window, campaign
    # multiplier 0.15 at campaign_window, percent 5 at both.
    findings = (
        portfolio("Portfolio attributed ROAS", "0.20"),
        campaign("Fictional Campaign Cedar", "-0.20"),
        campaign("Fictional Campaign Alder", "-0.001"),
        portfolio("Portfolio attributed ROAS", "20", percent),
        campaign("Fictional Campaign Cedar", "-10", percent),
    )

    admitted = admitted_predicate_findings(
        contract,  # type: ignore[arg-type]
        InsightEvidenceBundle(
            (InsightEvidenceInput.PORTFOLIO_DIRECTION, InsightEvidenceInput.CAMPAIGN_COMPARISONS),
            findings,
        ),
        InsightHorizonRole.TACTICAL_REVIEW,
    )
    silent_without_direction = admitted_predicate_findings(
        contract,  # type: ignore[arg-type]
        InsightEvidenceBundle((InsightEvidenceInput.CAMPAIGN_COMPARISONS,), findings),
        InsightHorizonRole.TACTICAL_REVIEW,
    )

    assert {finding.name for finding in admitted} == {
        "absolute-change: Portfolio attributed ROAS",
        "absolute-change: Fictional Campaign Cedar",
        "percentage-change: Portfolio attributed ROAS",
        "percentage-change: Fictional Campaign Cedar",
    }
    assert silent_without_direction == ()


@pytest.mark.covers("ADR-068")
def test_next_check_requires_declared_and_available_delivery_follow_up_evidence() -> None:
    contract = _contracts()[InsightSlotId.NEXT_CHECK]
    roas = partial(
        _finding,
        unit=FindingUnit.MULTIPLIER,
        metric="roas_attributed_14d",
        inputs=(InsightEvidenceInput.DETAIL_COMPARISONS,),
    )
    findings = (
        roas("attributed ROAS", "0.50"),
        roas("attributed ROAS", "25", InsightFindingKind.PERCENTAGE_CHANGE),
    )
    evidence = (InsightEvidenceInput.DETAIL_COMPARISONS, InsightEvidenceInput.DELIVERY_FOLLOW_UP)

    admitted = admitted_predicate_findings(
        contract,  # type: ignore[arg-type]
        InsightEvidenceBundle(evidence, findings, (DeliveryFollowUpEvidence.CLICKS,)),
        InsightHorizonRole.TACTICAL_REVIEW,
    )
    silent_without_available_follow_up = admitted_predicate_findings(
        contract,  # type: ignore[arg-type]
        InsightEvidenceBundle(evidence, findings),
        InsightHorizonRole.TACTICAL_REVIEW,
    )

    assert len(admitted) == 2
    assert silent_without_available_follow_up == ()


def _horizon_materiality() -> InsightMateriality:
    """Diverging per-horizon thresholds at one basis, with a per-metric row for clicks."""
    rows = HorizonThresholds(Decimal("5"), Decimal("1"), Decimal("2"))
    return InsightMateriality(
        {
            _CAMPAIGN: BasisThresholds(
                {
                    FindingUnit.CURRENCY: rows,
                    FindingUnit.PERCENT: rows,
                    FindingUnit.COUNT: HorizonThresholds(
                        Decimal("3"), Decimal("40"), Decimal("150")
                    ),
                    FindingUnit.RATE_PP: HorizonThresholds(
                        Decimal("0.5"), Decimal("0.3"), Decimal("0.2")
                    ),
                },
                {"clicks": HorizonThresholds(Decimal("40"), Decimal("500"), Decimal("2000"))},
            )
        },
        NearZeroPriors(
            {MetricScale.CURRENCY: Decimal("0.1"), MetricScale.RATE_PP: Decimal("0.5")},
            {"cpc": Decimal("0.05")},
        ),
    )


def _horizon_contract() -> InsightContract:
    """Build a minimal contract whose per-horizon thresholds genuinely diverge."""
    return InsightContract(
        id=InsightSlotId.CONTRIBUTION,
        version=1,
        purpose="Explain one fictional contribution.",
        display_name="Contribution (fictional)",
        output_class=InsightOutputClass.PERSPECTIVE,
        allowed_inputs=("fictional deterministic finding",),
        deterministic_findings=(
            InsightFindingKind.ABSOLUTE_CHANGE,
            InsightFindingKind.PERCENTAGE_CHANGE,
        ),
        predicate=InsightSlotPredicate(
            (InsightEvidenceInput.CAMPAIGN_COMPARISONS,),
            (InsightFindingKind.ABSOLUTE_CHANGE,),
            _horizon_materiality(),
            (),
        ),
        silence_condition="No fictional finding is material.",
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


@pytest.mark.covers("ADR-076")
def test_selection_applies_the_declared_threshold_of_the_window_horizon() -> None:
    """One unit-blind minimum no longer serves 1-day and 14-day windows alike."""
    contract = _horizon_contract()
    evidence = InsightEvidenceBundle(
        (InsightEvidenceInput.CAMPAIGN_COMPARISONS,),
        (_finding("Fictional Campaign Cedar", "3"),),
    )

    tactical = admitted_predicate_findings(contract, evidence, InsightHorizonRole.TACTICAL_REVIEW)
    pulse = admitted_predicate_findings(contract, evidence, InsightHorizonRole.OPERATIONAL_PULSE)
    pattern = admitted_predicate_findings(
        contract, evidence, InsightHorizonRole.PATTERN_CONFIRMATION
    )

    # The same 3-unit change clears the 7D threshold (1) and the 14D threshold (2)
    # but not the 1D threshold (5): the 1D bias toward silence is threshold-owned.
    assert [finding.name for finding in tactical] == ["Fictional Campaign Cedar"]
    assert [finding.name for finding in pattern] == ["Fictional Campaign Cedar"]
    assert pulse == ()


@pytest.mark.covers("ADR-076")
def test_percentage_findings_against_near_zero_priors_are_suppressed_at_selection() -> None:
    """A percentage against a vanishing prior is a degenerate rate the model never sees."""
    contract = _horizon_contract()
    percent = InsightFindingKind.PERCENTAGE_CHANGE
    degenerate = _finding("Fictional Campaign Alder", "900", percent, prior="0.05")
    grounded = _finding("Fictional Campaign Cedar", "12", percent, prior="40")
    unstamped = _finding("Fictional Campaign Birch", "15", percent)
    absolute_with_tiny_prior = _finding("Fictional Campaign Rowan", "6", prior="0.05")
    admitted = admitted_predicate_findings(
        replace(
            contract,
            predicate=replace(
                contract.predicate,
                required_finding_kinds=(
                    InsightFindingKind.ABSOLUTE_CHANGE,
                    InsightFindingKind.PERCENTAGE_CHANGE,
                ),
            ),
        ),
        InsightEvidenceBundle(
            (InsightEvidenceInput.CAMPAIGN_COMPARISONS,),
            (degenerate, grounded, unstamped, absolute_with_tiny_prior),
        ),
        InsightHorizonRole.TACTICAL_REVIEW,
    )

    # The bound (0.1) suppresses only the percentage finding whose prior vanished;
    # a percentage with a real prior, an unstamped finding, and an absolute change
    # over the same tiny prior all survive.
    assert {finding.name for finding in admitted} == {
        "Fictional Campaign Cedar",
        "Fictional Campaign Birch",
        "Fictional Campaign Rowan",
    }


@pytest.mark.covers("ADR-076")
def test_a_rate_change_is_compared_in_percentage_points() -> None:
    """0.004 is 0.4 pp: it clears a 0.3 pp row, and 0.002 (0.2 pp) does not."""
    materiality = _horizon_materiality()
    rate = partial(_finding, unit=FindingUnit.RATE_PP, metric="ctr")
    tactical = InsightHorizonRole.TACTICAL_REVIEW

    assert finding_is_material(materiality, rate("CTR", "0.004"), tactical)
    assert not finding_is_material(materiality, rate("CTR", "0.002"), tactical)


@pytest.mark.covers("ADR-076")
def test_a_rate_prior_is_bounded_in_percentage_points() -> None:
    """A prior CTR of 0.004 is 0.4 pp, at or under the 0.5 pp bound; 0.006 is not."""
    materiality = _horizon_materiality()
    percent = InsightFindingKind.PERCENTAGE_CHANGE
    rate = partial(_finding, unit=FindingUnit.RATE_PP, metric="ctr")
    tactical = InsightHorizonRole.TACTICAL_REVIEW

    assert not finding_is_material(materiality, rate("CTR", "25", percent, prior="0.004"), tactical)
    assert finding_is_material(materiality, rate("CTR", "25", percent, prior="0.006"), tactical)


@pytest.mark.covers("ADR-076")
def test_a_per_metric_row_takes_precedence_over_its_unit_row() -> None:
    """Clicks meet their own row (500), not the count row (40) other counts meet."""
    materiality = _horizon_materiality()
    tactical = InsightHorizonRole.TACTICAL_REVIEW
    count = partial(_finding, unit=FindingUnit.COUNT)

    assert finding_is_material(materiality, count("Purchases", "40", metric="purchases"), tactical)
    assert not finding_is_material(materiality, count("Clicks", "499", metric="clicks"), tactical)
    assert finding_is_material(materiality, count("Clicks", "500", metric="clicks"), tactical)
    # The clicks row sizes amounts only: a clicks percentage change meets the percent row (1).
    percent_clicks = count("Clicks", "2", InsightFindingKind.PERCENTAGE_CHANGE, metric="clicks")
    assert finding_is_material(materiality, percent_clicks, tactical)


@pytest.mark.covers("ADR-076")
def test_an_undeclared_basis_unit_or_prior_scale_fails_closed() -> None:
    """A finding the contract declares nothing for is a defect, never a silent pass."""
    materiality = _horizon_materiality()
    tactical = InsightHorizonRole.TACTICAL_REVIEW
    percent = InsightFindingKind.PERCENTAGE_CHANGE
    undeclared = (
        _finding("Portfolio spend", "9000", basis=OperandBasis.PORTFOLIO_WINDOW),
        _finding("ROAS", "0.5", unit=FindingUnit.MULTIPLIER, metric="roas_attributed_14d"),
        _finding(
            "ROAS",
            "20",
            percent,
            unit=FindingUnit.MULTIPLIER,
            metric="roas_attributed_14d",
            prior="2",
        ),
        _finding("CPM", "20", percent, unit=FindingUnit.UNIT_PRICE, metric="cpm", prior="30"),
    )
    # The positive control: a declared unit price bound resolves.
    assert finding_is_material(
        materiality,
        _finding("CPC", "20", percent, unit=FindingUnit.UNIT_PRICE, metric="cpc", prior="0.8"),
        tactical,
    )
    for item in undeclared:
        with pytest.raises(InvariantError) as raised:
            finding_is_material(materiality, item, tactical)
        assert raised.value.code == MATERIALITY_UNDECLARED


@pytest.mark.covers("ADR-076")
def test_a_prior_scale_is_declared_exactly_on_a_percentage_finding() -> None:
    finding = DeterministicFinding("Spend", Decimal("10"))
    with pytest.raises(ValueError, match="prior scale"):
        PredicateFinding(
            finding,
            InsightFindingKind.PERCENTAGE_CHANGE,
            _INPUTS,
            FindingUnit.PERCENT,
            _CAMPAIGN,
            "ad_spend",
        )
    with pytest.raises(ValueError, match="prior scale"):
        PredicateFinding(
            finding,
            InsightFindingKind.ABSOLUTE_CHANGE,
            _INPUTS,
            FindingUnit.CURRENCY,
            _CAMPAIGN,
            "ad_spend",
            MetricScale.CURRENCY,
        )
    with pytest.raises(ValueError, match="name their metric"):
        PredicateFinding(
            finding,
            InsightFindingKind.ABSOLUTE_CHANGE,
            _INPUTS,
            FindingUnit.CURRENCY,
            _CAMPAIGN,
            " ",
        )


@pytest.mark.covers("ADR-076")
def test_materiality_values_are_validated_at_construction() -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        HorizonThresholds(Decimal("0"), Decimal("1"), Decimal("1"))
    with pytest.raises(ValueError, match="per metric"):
        NearZeroPriors({MetricScale.UNIT_PRICE: Decimal("0.05")})
    with pytest.raises(ValueError, match="non-negative"):
        NearZeroPriors({MetricScale.COUNT: Decimal("-1")})
    with pytest.raises(ValueError, match="at least one threshold"):
        BasisThresholds({})
    with pytest.raises(ValueError, match="at least one operand basis"):
        InsightMateriality({}, NearZeroPriors({}))


@pytest.mark.covers("ADR-068")
def test_detail_derivation_classifies_missing_delivery_evidence_as_ineligible() -> None:
    """Never-evaluated delivery-bound slots must name their missing inputs.

    A detail served with an insufficient delivery classifies next-check and funnel
    as ineligible, while the fully evidenced slots stay admitted or evaluated-quiet.
    """
    detail, complete_delivery = detail_fixture()
    contracts = {contract.id: contract for _, contract in load_insight_contracts(ROOT)}
    scope = AuthorizedAnalyticalScope(frozenset({CommerceScopeKey("commerce-fictional")}))
    insufficient_delivery = CampaignDelivery(
        "insufficient",
        tuple(CampaignDeliveryValue(slot, None) for slot in CampaignDeliverySlot),
        complete_delivery.comparison_period,
        tuple(CampaignDeliveryValue(slot, None) for slot in CampaignDeliverySlot),
        (),
        (),
        complete_delivery.dsp_quality,
        campaign_delivery_missing_statuses(),
        complete_delivery.recovery,
    )

    def derive(delivery: CampaignDelivery):
        return detail_insight_request_derivation(
            DetailInsightRequestDerivationInput(
                detail=detail,
                delivery=delivery,
                subject="campaign-fictional",
                scope=scope,
                contracts=contracts,
            )
        )

    evidenced = derive(complete_delivery)
    unevidenced = derive(insufficient_delivery)

    # With complete delivery evidence, nothing on this artifact is ineligible.
    assert not evidenced.missing_evidence_by_slot
    # Without delivery values the two delivery-bound slots name their missing inputs.
    assert unevidenced.missing_evidence_by_slot == {
        InsightSlotId.FUNNEL: (InsightEvidenceInput.DELIVERY_METRICS,),
        InsightSlotId.NEXT_CHECK: (InsightEvidenceInput.DELIVERY_FOLLOW_UP,),
    }
    # The series- and comparison-bound slots keep their evidence and stay evaluated.
    untouched = {InsightSlotId.TREND_SHAPE, InsightSlotId.EFFICIENCY_DRIFT}
    evaluated = {request.key.slot_id for request in unevidenced.requests} | set(
        unevidenced.silence_reasons
    )
    assert untouched <= evaluated
    # Every admitted request carries the typed profile the served window derives.
    assert all(
        request.window_profile.role is horizon_role_for(detail.period.days)
        and request.window_profile.days == detail.period.days
        for request in evidenced.requests
    )
