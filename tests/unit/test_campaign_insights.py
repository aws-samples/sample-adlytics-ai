# SPDX-License-Identifier: MIT-0
"""Governed command-center insight contracts and pure domain invariants."""

from __future__ import annotations

import shutil
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
import yaml
from campaign_portfolio_support import (
    campaign_display_signature,
    complete_portfolio,
    portfolio_row,
)

from adapters.campaign_insight_requests import portfolio_insight_request_derivation
from adapters.jobs.contract_validation.insight_loader import (
    insight_contract_from_mapping,
    load_insight_contracts,
)
from domain.data_contracts.contract import ContractError
from domain.presentation.campaign_attribution import CampaignSelectionCapability
from domain.presentation.insights import (
    DeterministicFinding,
    DeterministicFindingRequest,
    DeterministicMoversSlotRequest,
    InsightAbsenceClass,
    InsightCalibrationKind,
    InsightEvidenceBundle,
    InsightEvidenceInput,
    InsightFindingKind,
    InsightFindingOperand,
    InsightGeneratedNarration,
    InsightHorizonRole,
    InsightOutputClass,
    InsightSlot,
    InsightSlotId,
    InsightSlotState,
    InsightWindowProfile,
    calculate_deterministic_findings,
    deterministic_movers_slot,
    deterministic_movers_text,
    evaluate_slot_predicate,
    gate_refused_insight_slots,
    horizon_role_for,
    ineligible_insight_slots,
    ineligible_reason,
    insight_slots_from_payload,
    insight_slots_payload,
    insight_window_profile,
    nonzero_movers,
    unavailable_insight_slots,
)
from domain.presentation.temporal import TemporalDisplayGrain
from domain.semantics.analytical_resolution import AuthorizedAnalyticalScope
from domain.semantics.identity import CommerceScopeKey
from domain.shared_kernel.values import DateRange

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.covers("ADR-068")
def test_governed_insight_contracts_declare_exact_roster_and_deterministic_movers() -> None:
    contracts = tuple(contract for _, contract in load_insight_contracts(ROOT))
    movers = next(contract for contract in contracts if contract.id is InsightSlotId.MOVERS)

    assert tuple(contract.id for contract in contracts) == tuple(InsightSlotId)
    assert movers.initial_fictional_prompt is None
    assert movers.deterministic_findings == (InsightFindingKind.RANKED_ABSOLUTE_CHANGE,)
    assert movers.structured_output_schema.fields == ("output_class", "sentences")
    assert all(contract.predicate.required_evidence_inputs for contract in contracts)
    assert all(contract.predicate.required_finding_kinds for contract in contracts)
    assert all(contract.predicate.materiality.bases for contract in contracts)


@pytest.mark.covers("ADR-076", "ADR-082")
def test_every_draft_judged_family_carries_a_draft_the_narration_shape_accepts() -> None:
    """Every family except the two generated ones judges a supplied fictional draft.

    The derived-figure families (ADR-082 §4) sit beside the original must-reject
    drafts; a draft the narration shape refused would abort its slot's calibration.
    """
    generated = {InsightCalibrationKind.KNOWN_CORRECT, InsightCalibrationKind.EXPLORATORY}
    for _, contract in load_insight_contracts(ROOT):
        kinds = {case.kind for case in contract.calibration_cases}
        assert kinds == set(InsightCalibrationKind), contract.id.value
        for case in contract.calibration_cases:
            assert (case.draft is None) is (case.kind in generated), (
                f"{contract.id.value}: {case.kind.value} draft presence"
            )
            if case.draft is not None:
                narration = InsightGeneratedNarration(contract.output_class, case.draft.sentences)
                assert narration.text


@pytest.mark.covers("ADR-082")
def test_the_derived_figure_families_declare_their_governed_dispositions() -> None:
    """A correct derivation must be accepted; a miscomputed or cross-basis one rejected."""
    for _, contract in load_insight_contracts(ROOT):
        cases = {case.kind: case for case in contract.calibration_cases}
        derived = cases[InsightCalibrationKind.DERIVED_FIGURE]
        assert derived.expected_disposition.value == "accept"
        assert len(derived.findings) >= 2
        for kind in (
            InsightCalibrationKind.MISCOMPUTED_DERIVATION,
            InsightCalibrationKind.INCOMPATIBLE_BASIS,
        ):
            assert cases[kind].expected_disposition.value == "reject"
            assert len(cases[kind].findings) >= 2


@pytest.mark.covers("ADR-068")
def test_movers_findings_stay_exact() -> None:
    findings = calculate_deterministic_findings(
        DeterministicFindingRequest(
            InsightFindingKind.RANKED_ABSOLUTE_CHANGE,
            (
                InsightFindingOperand("Fictional Birch", Decimal("120"), Decimal("20")),
                InsightFindingOperand("Fictional Aurora", Decimal("150"), Decimal("100")),
            ),
        )
    )
    shares = calculate_deterministic_findings(
        DeterministicFindingRequest(
            InsightFindingKind.SHARE_OF_TOTAL,
            (InsightFindingOperand("Fictional Cedar", Decimal("25")),),
            total=Decimal("100"),
        )
    )

    assert tuple((finding.name, finding.value) for finding in findings) == (
        ("Fictional Birch", Decimal("100")),
        ("Fictional Aurora", Decimal("50")),
    )
    assert shares == (DeterministicFinding("Fictional Cedar", Decimal("25.00")),)


@pytest.mark.covers("ADR-068")
def test_division_findings_quantize_to_two_decimals_and_differences_stay_exact() -> None:
    """The derivation seam owns display precision (ADR-068 amendment).

    Exact-decimal division emitted 28-digit rates whose verbatim citation
    overflowed the model's structured-output budget.
    """
    changes = calculate_deterministic_findings(
        DeterministicFindingRequest(
            InsightFindingKind.PERCENTAGE_CHANGE,
            (InsightFindingOperand("Fictional Larch", Decimal("4321.7700"), Decimal("7645.1300")),),
        )
    )
    shares = calculate_deterministic_findings(
        DeterministicFindingRequest(
            InsightFindingKind.SHARE_OF_TOTAL,
            (InsightFindingOperand("Fictional Rowan", Decimal("1")),),
            total=Decimal("3"),
        )
    )
    differences = calculate_deterministic_findings(
        DeterministicFindingRequest(
            InsightFindingKind.ABSOLUTE_CHANGE,
            (InsightFindingOperand("Fictional Larch", Decimal("4321.7700"), Decimal("7645.1300")),),
        )
    )

    assert changes == (DeterministicFinding("Fictional Larch", Decimal("-43.47")),)
    assert changes[0].value.as_tuple().exponent == -2
    assert shares == (DeterministicFinding("Fictional Rowan", Decimal("33.33")),)
    # The exact difference keeps its operands' served currency scale, never a division tail.
    assert differences == (DeterministicFinding("Fictional Larch", Decimal("-3323.3600")),)
    assert differences[0].value.as_tuple().exponent == -4


@pytest.mark.covers("ADR-068")
def test_insight_loader_rejects_incomplete_or_invalid_executable_predicates() -> None:
    raw = cast(
        "dict[str, object]",
        yaml.safe_load((ROOT / "contracts/insights/portfolio-divergence.yaml").read_text()),
    )
    predicate = cast("dict[str, object]", raw["predicate"])
    materiality = cast("dict[str, object]", predicate["materiality"])
    missing_predicate = dict(raw)
    missing_predicate.pop("predicate")
    bases = cast("dict[str, dict[str, object]]", materiality["bases"])
    campaign = bases["campaign_window"]
    per_unit = cast("dict[str, dict[str, str]]", campaign["per_unit"])

    def with_materiality(value: dict[str, object]) -> dict[str, object]:
        return {**raw, "predicate": {**predicate, "materiality": value}}

    def with_campaign_unit(unit: str, row: dict[str, str]) -> dict[str, object]:
        rows = {**campaign, "per_unit": {**per_unit, unit: row}}
        return with_materiality({**materiality, "bases": {**bases, "campaign_window": rows}})

    zero_threshold = with_campaign_unit(
        "multiplier", {**per_unit["multiplier"], "tactical-review": "0"}
    )
    missing_role = with_campaign_unit("percent", {"tactical-review": "5"})
    retired_minimum = with_materiality({**materiality, "minimum": "0.01"})
    unknown_basis = with_materiality(
        {**materiality, "bases": {**bases, "account_window": campaign}}
    )
    loose_basis = with_materiality(
        {**materiality, "bases": {**bases, "campaign_window": {**campaign, "extra": {}}}}
    )
    unit_price_per_scale = with_materiality(
        {**materiality, "near_zero_prior": {"unit_price": "0.05"}}
    )

    with pytest.raises(ContractError, match="fields are not exact"):
        insight_contract_from_mapping(missing_predicate, where="fictional")
    with pytest.raises(ContractError, match="finite positive decimal"):
        insight_contract_from_mapping(zero_threshold, where="fictional")
    with pytest.raises(ContractError, match="materiality: fields are not exact"):
        insight_contract_from_mapping(retired_minimum, where="fictional")
    with pytest.raises(ContractError, match="bases"):
        insight_contract_from_mapping(unknown_basis, where="fictional")
    with pytest.raises(ContractError, match="campaign_window: fields are not exact"):
        insight_contract_from_mapping(loose_basis, where="fictional")
    with pytest.raises(ContractError, match="every horizon role"):
        insight_contract_from_mapping(missing_role, where="fictional")
    with pytest.raises(ContractError, match="unit_price"):
        insight_contract_from_mapping(unit_price_per_scale, where="fictional")
    # The positive control: the committed block parses.
    assert insight_contract_from_mapping(raw, where="fictional").predicate.materiality.bases


@pytest.mark.covers("ADR-067")
def test_every_insight_contract_fails_closed_without_its_display_name(tmp_path: Path) -> None:
    """Prompt administration serves each slot's display_name, so no contract may omit it."""
    paths = sorted((ROOT / "contracts/insights").glob("*.yaml"))
    assert paths
    for path in paths:
        raw = cast("dict[str, object]", yaml.safe_load(path.read_text()))
        # Positive control: the committed contract parses with its name.
        assert insight_contract_from_mapping(raw, where=path.name).display_name.strip()
        missing = {key: value for key, value in raw.items() if key != "display_name"}
        with pytest.raises(ContractError, match="insight contract fields are not exact"):
            insight_contract_from_mapping(missing, where=path.name)
        with pytest.raises(ContractError, match=r"display_name: expected nonblank text"):
            insight_contract_from_mapping({**raw, "display_name": "   "}, where=path.name)
    # The directory load names the offending file, as it did for retention-shift at the D1 merge.
    shutil.copytree(ROOT / "contracts", tmp_path / "contracts")
    target = tmp_path / "contracts/insights/retention-shift.yaml"
    source = target.read_text()
    line = "display_name: Retention shift (Retention)\n"
    assert source.count(line) == 1
    target.write_text(source.replace(line, ""))
    with pytest.raises(ContractError, match=r"retention-shift\.yaml: insight contract fields"):
        load_insight_contracts(tmp_path)


@pytest.mark.covers("ADR-068")
def test_insight_loader_rejects_duplicate_yaml_predicate_keys(tmp_path: Path) -> None:
    directory = tmp_path / "contracts" / "insights"
    directory.mkdir(parents=True)
    source = (ROOT / "contracts/insights/portfolio-divergence.yaml").read_text()
    bound = '    near_zero_prior:\n      multiplier: "0.1"\n'
    assert source.count(bound) == 1
    directory.joinpath("portfolio-divergence.yaml").write_text(
        source.replace(bound, bound + '      multiplier: "1"\n')
    )

    with pytest.raises(ContractError, match="duplicate key"):
        load_insight_contracts(tmp_path)


@pytest.mark.covers("ADR-068")
def test_insight_slot_payload_round_trips_the_declared_absence_taxonomy() -> None:
    slots = (
        gate_refused_insight_slots(
            (InsightSlotId.CONTRIBUTION,),
            artifact_revision="sha256:revision-fictional",
            reasons={
                InsightSlotId.CONTRIBUTION: (
                    "No campaign contribution exceeds the declared materiality threshold."
                )
            },
        )
        + ineligible_insight_slots(
            (InsightSlotId.DIVERGENCE,),
            artifact_revision="sha256:revision-fictional",
            missing_by_slot={InsightSlotId.DIVERGENCE: (InsightEvidenceInput.PORTFOLIO_DIRECTION,)},
        )
        + unavailable_insight_slots(
            (InsightSlotId.EXPOSURE,),
            artifact_revision="sha256:revision-fictional",
            absence_class=InsightAbsenceClass.GENERATION_PENDING,
        )
        + unavailable_insight_slots(
            (InsightSlotId.MOVERS,), artifact_revision="sha256:revision-fictional"
        )
    )
    expected = (
        InsightSlotId.CONTRIBUTION,
        InsightSlotId.DIVERGENCE,
        InsightSlotId.EXPOSURE,
        InsightSlotId.MOVERS,
    )

    payload = insight_slots_payload(slots)

    # Every slot here declares an absence, so every encoding carries the pair.
    assert all(len(item) == 9 for item in payload)
    assert insight_slots_from_payload(payload, expected=expected) == slots
    classes = tuple(cast("str", item["absence_class"]) for item in payload)
    assert classes == ("evaluated-quiet", "ineligible", "generation-pending", "out-of-slice")
    assert all(isinstance(item["absence_reason"], str) for item in payload)
    ineligible = cast("str", payload[1]["absence_reason"])
    assert "a portfolio-level direction" in ineligible
    assert "not available for this view and window" in ineligible


@pytest.mark.covers("ADR-068")
def test_slot_payload_carries_the_absence_pair_exactly_when_an_absence_is_declared() -> None:
    """One shape rule keeps exact readback sound over retained pre-taxonomy bodies."""
    filled = deterministic_movers_slot(
        DeterministicMoversSlotRequest(
            artifact_revision="sha256:revision-fictional",
            findings=(DeterministicFinding("Fictional Campaign", Decimal("5")),),
            silence_reason="No server-ranked absolute change exceeds the declared threshold.",
            currency_code="USD",
        )
    )
    assert filled.text == "Fictional Campaign changed by 5.00 USD."
    undeclared = InsightSlot(
        InsightSlotId.CONTRIBUTION,
        InsightOutputClass.PERSPECTIVE,
        InsightSlotState.SILENT,
        None,
        None,
        "sha256:revision-fictional",
        "v1",
    )

    payload = insight_slots_payload((undeclared, filled))

    assert all(len(item) == 7 for item in payload)
    assert all("absence_class" not in item for item in payload)
    decoded = insight_slots_from_payload(
        payload, expected=(InsightSlotId.CONTRIBUTION, InsightSlotId.MOVERS)
    )
    assert decoded == (undeclared, filled)
    # The pair present with a null class is neither legal shape: it would re-encode
    # as the base shape and break exact readback.
    with pytest.raises(ValueError, match="not exact"):
        insight_slots_from_payload(
            [{**payload[0], "absence_class": None}],
            expected=(InsightSlotId.CONTRIBUTION,),
        )
    with pytest.raises(ValueError, match="enum is unsupported"):
        insight_slots_from_payload(
            [{**payload[0], "absence_class": None, "absence_reason": None}],
            expected=(InsightSlotId.CONTRIBUTION,),
        )
    with pytest.raises(ValueError, match="enum is unsupported"):
        insight_slots_from_payload(
            [{**payload[0], "absence_class": "unknown-class", "absence_reason": None}],
            expected=(InsightSlotId.CONTRIBUTION,),
        )


@pytest.mark.covers("ADR-068")
def test_insight_slot_invariants_bind_absence_fields_to_absent_states() -> None:
    with pytest.raises(ValueError, match="cannot declare an absence"):
        InsightSlot(
            id=InsightSlotId.CONTRIBUTION,
            output_class=InsightOutputClass.PERSPECTIVE,
            state=InsightSlotState.FILLED,
            text="A governed finding matters.",
            evidence_line="Read from the values this page already shows.",
            artifact_revision="sha256:revision-fictional",
            prompt_version="v1",
            absence_class=InsightAbsenceClass.EVALUATED_QUIET,
            absence_reason="Reason.",
        )
    with pytest.raises(ValueError, match="requires its absence class"):
        InsightSlot(
            id=InsightSlotId.CONTRIBUTION,
            output_class=InsightOutputClass.PERSPECTIVE,
            state=InsightSlotState.SILENT,
            text=None,
            evidence_line=None,
            artifact_revision="sha256:revision-fictional",
            prompt_version="v1",
            absence_reason="Reason without a class.",
        )
    with pytest.raises(ValueError, match="not-generated absence class"):
        unavailable_insight_slots(
            (InsightSlotId.CONTRIBUTION,),
            absence_class=InsightAbsenceClass.EVALUATED_QUIET,
        )


@pytest.mark.covers("ADR-068")
def test_slot_predicate_evaluation_separates_missing_evidence_from_quiet() -> None:
    contracts = {contract.id: contract for _, contract in load_insight_contracts(ROOT)}
    next_check = contracts[InsightSlotId.NEXT_CHECK]

    absent = evaluate_slot_predicate(next_check, None, InsightHorizonRole.TACTICAL_REVIEW)
    missing_follow_up = evaluate_slot_predicate(
        next_check,
        InsightEvidenceBundle((InsightEvidenceInput.DETAIL_COMPARISONS,), ()),
        InsightHorizonRole.TACTICAL_REVIEW,
    )

    # No offered bundle means every declared requirement is missing evidence.
    assert absent.findings == ()
    assert absent.missing_evidence == next_check.predicate.required_evidence_inputs
    assert missing_follow_up.findings == ()
    assert missing_follow_up.missing_evidence == (InsightEvidenceInput.DELIVERY_FOLLOW_UP,)
    with pytest.raises(ValueError, match="at least one missing evidence input"):
        ineligible_reason(())


@pytest.mark.covers("ADR-076")
def test_horizon_role_banding_is_total_over_every_window_length() -> None:
    """Every window declares a role — including custom and 30-day lengths.

    The ADR binds roles to the 1D/7D/14D presets; other lengths take the nearest
    question by banding so the typed profile is never silently omitted.
    """
    assert horizon_role_for(1) is InsightHorizonRole.OPERATIONAL_PULSE
    assert horizon_role_for(2) is InsightHorizonRole.TACTICAL_REVIEW
    assert horizon_role_for(7) is InsightHorizonRole.TACTICAL_REVIEW
    assert horizon_role_for(14) is InsightHorizonRole.PATTERN_CONFIRMATION
    assert horizon_role_for(30) is InsightHorizonRole.PATTERN_CONFIRMATION
    with pytest.raises(ValueError, match="positive window length"):
        horizon_role_for(0)


@pytest.mark.covers("ADR-076")
def test_window_profile_derives_from_the_served_window_and_pins_its_prior_pair() -> None:
    with_prior = insight_window_profile(
        DateRange(date(2026, 9, 22), date(2026, 9, 22)),
        DateRange(date(2026, 9, 21), date(2026, 9, 21)),
    )
    without_prior = insight_window_profile(DateRange(date(2026, 9, 8), date(2026, 9, 21)), None)

    assert with_prior.days == 1
    assert with_prior.grain is TemporalDisplayGrain.DAY
    assert with_prior.role is InsightHorizonRole.OPERATIONAL_PULSE
    assert (with_prior.prior_start, with_prior.prior_end) == ("2026-09-21", "2026-09-21")
    assert without_prior.days == 14
    assert without_prior.role is InsightHorizonRole.PATTERN_CONFIRMATION
    assert without_prior.prior_start is None and without_prior.prior_end is None
    with pytest.raises(ValueError, match="present together"):
        InsightWindowProfile(
            days=7,
            grain=TemporalDisplayGrain.DAY,
            prior_start="2026-09-01",
            prior_end=None,
            role=InsightHorizonRole.TACTICAL_REVIEW,
        )


@pytest.mark.covers("ADR-068")
def test_deterministic_movers_slot_names_its_silence_condition_when_quiet() -> None:
    silence = "No server-ranked absolute change exceeds the declared materiality threshold."

    quiet = deterministic_movers_slot(
        DeterministicMoversSlotRequest("sha256:revision-fictional", (), silence, None)
    )
    filled = deterministic_movers_slot(
        DeterministicMoversSlotRequest(
            artifact_revision="sha256:revision-fictional",
            findings=(DeterministicFinding("Fictional Campaign", Decimal("5")),),
            silence_reason=silence,
            currency_code="USD",
        )
    )

    assert quiet.state is InsightSlotState.SILENT
    assert quiet.absence_class is InsightAbsenceClass.EVALUATED_QUIET
    assert quiet.absence_reason == silence
    assert filled.state is InsightSlotState.FILLED
    assert filled.absence_class is None and filled.absence_reason is None
    with pytest.raises(ValueError, match="requires its silence condition"):
        DeterministicMoversSlotRequest("sha256:revision-fictional", (), " ", None)


@pytest.mark.covers("ADR-084")
def test_movers_text_writes_two_place_amounts_with_their_currency_code() -> None:
    """Spend changes in USD dollars: 1234.5 → 1,234.50; -0.125 → -0.13 (ties away from zero)."""
    findings = (
        DeterministicFinding("Fictional Campaign A", Decimal("1234.5")),
        DeterministicFinding("Fictional Campaign B", Decimal("-0.125")),
        DeterministicFinding("Fictional Campaign C", Decimal("75459.6400")),
        DeterministicFinding("Fictional Campaign D", Decimal("1")),
    )

    text = deterministic_movers_text(findings, "USD")

    assert text == (
        "Fictional Campaign A changed by 1,234.50 USD; "
        "Fictional Campaign B changed by -0.13 USD; "
        "Fictional Campaign C changed by 75,459.64 USD"
    )


@pytest.mark.covers("ADR-084")
def test_movers_findings_require_their_currency_code_and_empty_findings_ignore_it() -> None:
    finding = (DeterministicFinding("Fictional Campaign", Decimal("5")),)

    with pytest.raises(ValueError, match="require their currency code"):
        DeterministicMoversSlotRequest("sha256:revision-fictional", finding, "Quiet.", None)
    quiet = DeterministicMoversSlotRequest("sha256:revision-fictional", (), "Quiet.", None)
    assert deterministic_movers_slot(quiet).state is InsightSlotState.SILENT


@pytest.mark.covers("ADR-068")
def test_w12_zero_change_rows_are_never_listed_as_movers() -> None:
    findings = (
        DeterministicFinding("Fictional Campaign A", Decimal("40")),
        DeterministicFinding("Fictional Campaign B", Decimal("0")),
        DeterministicFinding("Fictional Campaign C", Decimal("-0.00")),
        DeterministicFinding("Fictional Campaign D", Decimal("-3")),
    )

    assert nonzero_movers(findings) == (findings[0], findings[3])
    assert deterministic_movers_text(findings, "USD") == (
        "Fictional Campaign A changed by 40.00 USD; Fictional Campaign D changed by -3.00 USD"
    )
    # Positive control: a nonzero row in a zero row's place is listed.
    moved = (*findings[:1], DeterministicFinding("Fictional Campaign B", Decimal("0.01")))
    assert "Fictional Campaign B changed by 0.01 USD" in deterministic_movers_text(moved, "USD")


@pytest.mark.covers("ADR-068")
def test_w12_all_zero_movers_are_the_declared_silence() -> None:
    silence = "No server-ranked absolute change exceeds the declared materiality threshold."
    zeros = tuple(
        DeterministicFinding(f"Fictional Campaign {index}", Decimal(0)) for index in range(3)
    )

    slot = deterministic_movers_slot(
        DeterministicMoversSlotRequest("sha256:revision-fictional", zeros, silence, "USD")
    )

    assert slot.state is InsightSlotState.SILENT
    assert slot.text is None
    assert slot.absence_class is InsightAbsenceClass.EVALUATED_QUIET
    assert slot.absence_reason == silence


@pytest.mark.covers("ADR-068")
def test_w12_the_portfolio_movers_builder_drops_unmoved_campaigns() -> None:
    period = DateRange(date(2026, 9, 29), date(2026, 10, 5))
    spends = (
        (Decimal(900), Decimal(500)),
        (Decimal(700), Decimal(700)),
        (Decimal(400), Decimal(600)),
    )
    rows = tuple(
        portfolio_row(
            campaign_display_signature(f"Fictional Campaign {index}"),
            CampaignSelectionCapability(f"baked:{index:05d}", datetime(2027, 1, 1, tzinfo=UTC)),
            period,
            spend,
            prior_spend=prior,
        )
        for index, (spend, prior) in enumerate(spends)
    )

    _, movers = portfolio_insight_request_derivation(
        artifact=complete_portfolio(period, rows),
        scope=AuthorizedAnalyticalScope(frozenset({CommerceScopeKey("commerce-fictional")})),
        contracts={contract.id: contract for _, contract in load_insight_contracts(ROOT)},
    )

    assert [item.value for item in movers] == [Decimal(400), Decimal(-200)]
    assert all("Fictional Campaign 1" not in item.name for item in movers)


@pytest.mark.covers("ADR-068")
def test_every_evidence_input_carries_a_customer_legible_label() -> None:
    """A member without a label would fail at serve time inside an ineligible reason."""
    for member in InsightEvidenceInput:
        assert "not available for this view and window" in ineligible_reason((member,))
