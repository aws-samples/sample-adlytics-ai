# SPDX-License-Identifier: MIT-0
"""Boundary and immutability rules of the insight domain this branch added (ADR-076).

Each test pins a rule the 2026-10-06 mutation review (ADR-013) found unasserted: an
error-code number, a threshold or
near-zero boundary, an immutable record, a structural next-step refusal, the canonical
input form, or a tier rule on the generation request.
"""

from __future__ import annotations

import dataclasses
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from insight_key_support import FICTIONAL_DEEP_INPUT_REVISION, PENDING_INPUT_DIGEST, card_key

from adapters.jobs.contract_validation.insight_loader import load_insight_contracts
from domain.presentation.insights import (
    MATERIALITY_UNDECLARED,
    QUIET_COPY_UNDECLARED,
    BasisThresholds,
    DeterministicFinding,
    FindingUnit,
    HorizonThresholds,
    InsightCacheKey,
    InsightContextEntry,
    InsightEvidenceBundle,
    InsightEvidenceInput,
    InsightFindingKind,
    InsightGenerationRequest,
    InsightHorizonRole,
    InsightMateriality,
    InsightModelRoute,
    InsightNextStep,
    InsightNextStepKind,
    InsightNextStepOption,
    InsightOutputClass,
    InsightPromptActivation,
    InsightPromptTier,
    InsightSlotId,
    InsightWindowProfile,
    MetricScale,
    NearZeroPriors,
    OperandBasis,
    PredicateAdmission,
    PredicateFinding,
    PredicateQuietReason,
    canonical_input_json,
    next_step_from_payload,
    predicate_admission,
    resolve_next_step,
)
from domain.presentation.next_steps import AssistantTarget, PresetTarget
from domain.presentation.temporal import TemporalDisplayGrain
from domain.shared_kernel.errors import ErrorCode

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-076")]
ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = {contract.id: contract for _, contract in load_insight_contracts(ROOT)}
TACTICAL = InsightHorizonRole.TACTICAL_REVIEW
_PROFILE = InsightWindowProfile(7, TemporalDisplayGrain.DAY, "2026-09-22", "2026-09-28", TACTICAL)
_PRESET = PresetTarget("campaigns", "14d")
_OPTION = InsightNextStepOption("s1", InsightNextStepKind.OPEN_PRESET, _PRESET, "Show 14 days")


def test_the_new_insight_error_codes_keep_their_numbers() -> None:
    assert ErrorCode("INSIGHT", 13) == MATERIALITY_UNDECLARED
    assert ErrorCode("INSIGHT", 15) == QUIET_COPY_UNDECLARED


def test_a_threshold_must_be_positive_and_a_near_zero_bound_may_be_zero() -> None:
    for value in (Decimal(0), Decimal(-1)):
        with pytest.raises(ValueError, match="finite and positive"):
            HorizonThresholds(value, Decimal(1), Decimal(1))
    assert NearZeroPriors({MetricScale.COUNT: Decimal(0)}).per_scale[MetricScale.COUNT] == 0
    with pytest.raises(ValueError, match="non-negative"):
        NearZeroPriors({MetricScale.COUNT: Decimal("-0.01")})


def test_materiality_and_next_step_records_are_immutable() -> None:
    row = HorizonThresholds(Decimal(1), Decimal(1), Decimal(1))
    basis = BasisThresholds({FindingUnit.COUNT: row})
    near_zero = NearZeroPriors({})
    records = (
        basis,
        near_zero,
        InsightMateriality({OperandBasis.CAMPAIGN_WINDOW: basis}, near_zero),
        _OPTION,
        resolve_next_step((_OPTION,), "s1", "A reason."),
        PredicateAdmission((), PredicateQuietReason.BELOW_THRESHOLD),
    )
    for record in records:
        name = dataclasses.fields(record)[0].name
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(record, name, getattr(record, name))


def test_a_contract_refuses_a_repeated_next_step_kind() -> None:
    contract = CONTRACTS[InsightSlotId.CONTRIBUTION]
    kind = InsightNextStepKind.ASK_ASSISTANT
    assert replace(contract, next_step_kinds=(kind,)).next_step_kinds == (kind,)
    with pytest.raises(ValueError, match="next-step kinds must be unique"):
        replace(contract, next_step_kinds=(kind, kind))


def test_next_step_targets_and_ids_are_structurally_required() -> None:
    for target in (None, AssistantTarget("Why did ad spend rise from Sep 2-8 to Sep 9-15?")):
        with pytest.raises(ValueError, match="requires its own target shape"):
            InsightNextStepOption("s1", InsightNextStepKind.OPEN_PRESET, target, "Open")
        with pytest.raises(ValueError, match="requires its own target shape"):
            InsightNextStep(InsightNextStepKind.OPEN_PRESET, target, "Open", "A reason.")
    with pytest.raises(ValueError, match="takes no target"):
        InsightNextStepOption("s1", InsightNextStepKind.OPEN_EVIDENCE_STATUS, _PRESET, "See why")
    for step_id, label in ((" ", "Open"), ("s1", " ")):
        with pytest.raises(ValueError, match="requires an id and a label"):
            InsightNextStepOption(step_id, InsightNextStepKind.OPEN_PRESET, _PRESET, label)


def test_an_id_that_sorts_before_every_option_is_still_refused() -> None:
    with pytest.raises(ValueError, match="not one of this card's served options"):
        resolve_next_step((_OPTION,), "s0", "A reason.")


def test_a_stored_step_with_a_non_text_field_is_malformed() -> None:
    payload = {
        "kind": "open_preset",
        "label": "Open",
        "reason": "A reason.",
        "target": {"page_id": "campaigns", "preset_id": "14d"},
    }
    for field in ("kind", "label", "reason"):
        with pytest.raises(ValueError, match="malformed"):
            next_step_from_payload({**payload, field: 1})


def test_the_canonical_input_form_is_sorted_compact_and_unescaped() -> None:
    assert canonical_input_json({"b": "Fictional — campaign", "a": [1, 2]}) == (
        '{"a":[1,2],"b":"Fictional — campaign"}'
    )


def _generation(**changes: object) -> InsightGenerationRequest:
    fields: dict[str, object] = {
        "output_class": InsightOutputClass.PERSPECTIVE,
        "purpose": "Explain one server-owned contribution.",
        "findings": (DeterministicFinding("Fictional Campaign", Decimal(5)),),
        "window_profile": _PROFILE,
        "context": (),
    }
    key = card_key(
        InsightCacheKey(
            "analytical-scope-v1:fictional",
            "campaign-portfolio",
            "2026-09-29",
            "2026-10-05",
            "revision-fictional",
            "v1",
            InsightSlotId.CONTRIBUTION,
            PENDING_INPUT_DIGEST,
        ),
        SimpleNamespace(**fields),
    )
    fields.update(changes)
    return InsightGenerationRequest(
        key=fields.pop("key", key),  # type: ignore[arg-type]
        prompt=InsightPromptActivation(
            InsightSlotId.CONTRIBUTION, "v1", "A fictional prompt.", InsightModelRoute.QUICK
        ),
        **fields,  # type: ignore[arg-type]
    )


def _deep_key() -> InsightCacheKey:
    return replace(
        _generation().key,
        tier=InsightPromptTier.DEEP,
        deep_input_revision=FICTIONAL_DEEP_INPUT_REVISION,
    )


def test_a_card_generation_refuses_any_digest_but_its_own() -> None:
    assert _generation().key.input_digest != "f" * 64
    for digest in ("0" * 64, "f" * 64):
        with pytest.raises(ValueError, match="must match its cache input digest"):
            _generation(key=replace(_generation().key, input_digest=digest))


def test_a_deep_generation_keeps_the_card_digest_beside_its_enriched_input() -> None:
    """Deep input is enriched after the lookup, so its key names the card's input."""
    enriched = (InsightContextEntry("attribution timing", "Fictional enrichment."),)
    assert _generation(key=_deep_key(), context=enriched).key.tier is InsightPromptTier.DEEP


def test_only_the_card_tier_offers_next_steps() -> None:
    assert _generation(next_step_options=(_OPTION,)).next_step_options == (_OPTION,)
    with pytest.raises(ValueError, match="only the card tier offers next steps"):
        _generation(key=_deep_key(), next_step_options=(_OPTION,))


def _returns_admission(prior: str) -> PredicateAdmission:
    """Judge a material returns change beside a percentage exactly at its threshold (1.5)."""
    contract = CONTRACTS[InsightSlotId.RETURNS_PRESSURE]
    inputs = (InsightEvidenceInput.RETAIL_TOTALS,)
    findings = (
        PredicateFinding(
            DeterministicFinding("Customer returns change", Decimal(10)),
            InsightFindingKind.ABSOLUTE_CHANGE,
            inputs,
            FindingUnit.COUNT,
            OperandBasis.RETAIL_WINDOW,
            "customer_returns",
        ),
        PredicateFinding(
            DeterministicFinding("Return rate change", Decimal("1.5")),
            InsightFindingKind.PERCENTAGE_CHANGE,
            inputs,
            FindingUnit.PERCENT,
            OperandBasis.RETAIL_WINDOW,
            "return_rate",
            MetricScale.RATE_PP,
            Decimal(prior),
        ),
    )
    return predicate_admission(contract, InsightEvidenceBundle(inputs, findings), TACTICAL)


def test_a_rate_prior_at_its_near_zero_bound_is_suppressed_and_just_above_it_is_not() -> None:
    """The bound is 0.5 percentage points; a rate prior is compared in points (x 100)."""
    contract = CONTRACTS[InsightSlotId.RETURNS_PRESSURE]
    bound = contract.predicate.materiality.prior_bound_for(MetricScale.RATE_PP, "return_rate")
    assert bound == Decimal("0.5")
    # At the bound (0.005 = 0.5 pp): suppressed, and a threshold-sized move names that reason.
    assert _returns_admission("0.005").quiet_reason is PredicateQuietReason.NEAR_ZERO_PRIOR
    # Just above it (0.501 pp): the 1.5 % move meets its threshold exactly and is admitted.
    admitted = _returns_admission("0.00501")
    assert admitted.quiet_reason is None and len(admitted.findings) == 2


def test_a_parsed_step_id_resolves_by_value() -> None:
    """A model-chosen id is an equal string, never the server's own string object."""
    assert resolve_next_step((_OPTION,), "".join(("s", "1")), "A reason.").target == _PRESET


def test_an_input_digest_is_exactly_64_characters() -> None:
    key = _generation().key
    for digest in ("f" * 63, "f" * 65):
        with pytest.raises(ValueError, match="64 lowercase hex"):
            replace(key, input_digest=digest)


def test_a_generation_refuses_repeated_next_step_ids() -> None:
    with pytest.raises(ValueError, match="next-step options must have unique ids"):
        _generation(next_step_options=(_OPTION, _OPTION))
