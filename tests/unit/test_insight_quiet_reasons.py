# SPDX-License-Identifier: MIT-0
"""W13: an evaluated-quiet card serves why Gate-1 went quiet, in customer words.

The served reason stops being the contract's `silence_condition` (ADR-068 2026-10-05
amendment): Gate-1 also goes quiet on a missing input, a missing kind or a near-zero prior,
and one governance sentence would be false for those.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from adapters.jobs.contract_validation.insight_loader import load_insight_contracts
from domain.presentation.insights import (
    INSIGHT_MODEL_SILENCE_REASON,
    INSIGHT_QUIET_COPY,
    QUIET_COPY_UNDECLARED,
    DeterministicFinding,
    FindingUnit,
    InsightContract,
    InsightEvidenceBundle,
    InsightFindingKind,
    InsightHorizonRole,
    InsightSlotId,
    MetricScale,
    OperandBasis,
    PredicateFinding,
    PredicateQuietReason,
    SlotPredicateEvaluation,
    evaluate_slot_predicate,
    insight_quiet_reason,
    predicate_admission,
)
from domain.shared_kernel.errors import InvariantError

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-068")]
ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = {contract.id: contract for _, contract in load_insight_contracts(ROOT)}
_CONTRACT_WORDS = (
    "threshold",
    "materiality",
    "predicate",
    "contract",
    "finding",
    "gate",
    "operand",
    "silence",
)
_TACTICAL = InsightHorizonRole.TACTICAL_REVIEW
_INPUTS = CONTRACTS[InsightSlotId.CONTRIBUTION].predicate.required_evidence_inputs


def _reachable(contract: InsightContract) -> tuple[PredicateQuietReason, ...]:
    """Return the reasons the contract can reach: those whose trigger it declares."""
    reasons = [
        PredicateQuietReason.MISSING_INPUT,
        PredicateQuietReason.MISSING_KIND,
        PredicateQuietReason.BELOW_THRESHOLD,
    ]
    if InsightFindingKind.PERCENTAGE_CHANGE in contract.predicate.required_finding_kinds:
        reasons.append(PredicateQuietReason.NEAR_ZERO_PRIOR)
    return tuple(reasons)


_REACHABLE = [
    (slot, reason)
    for slot, contract in CONTRACTS.items()
    if slot is not InsightSlotId.MOVERS
    for reason in _reachable(contract)
]


@pytest.mark.parametrize(
    ("slot", "reason"), _REACHABLE, ids=[f"{s.value}-{r.value}" for s, r in _REACHABLE]
)
def test_every_reachable_quiet_reason_has_customer_copy(
    slot: InsightSlotId, reason: PredicateQuietReason
) -> None:
    text = insight_quiet_reason(slot, reason)
    silence = CONTRACTS[slot].silence_condition
    assert text.strip()
    assert not any(word in text.lower() for word in _CONTRACT_WORDS), text
    assert text != silence
    assert text not in silence


def test_model_silence_has_its_own_customer_copy() -> None:
    assert INSIGHT_MODEL_SILENCE_REASON.strip()
    assert not any(word in INSIGHT_MODEL_SILENCE_REASON.lower() for word in _CONTRACT_WORDS)
    assert INSIGHT_MODEL_SILENCE_REASON not in INSIGHT_QUIET_COPY.values()


def test_an_undeclared_slot_reason_fails_closed() -> None:
    # Positive control: a declared pair resolves; the deterministic movers slot has none.
    assert insight_quiet_reason(InsightSlotId.CONTRIBUTION, PredicateQuietReason.MISSING_KIND)
    with pytest.raises(InvariantError) as raised:
        insight_quiet_reason(InsightSlotId.MOVERS, PredicateQuietReason.BELOW_THRESHOLD)
    assert raised.value.code == QUIET_COPY_UNDECLARED


def _spend(kind: InsightFindingKind, value: str, prior: str | None = None) -> PredicateFinding:
    percentage = kind is InsightFindingKind.PERCENTAGE_CHANGE
    return PredicateFinding(
        DeterministicFinding("Fictional Campaign Cedar", Decimal(value)),
        kind,
        _INPUTS,
        FindingUnit.PERCENT if percentage else FindingUnit.CURRENCY,
        OperandBasis.CAMPAIGN_WINDOW,
        "ad_spend",
        MetricScale.CURRENCY if percentage else None,
        prior_magnitude=None if prior is None else Decimal(prior),
    )


_ABS = InsightFindingKind.ABSOLUTE_CHANGE
_PCT = InsightFindingKind.PERCENTAGE_CHANGE
# Contribution at tactical-review: currency row 300, percent row 5, currency prior bound 5.
_CASES = {
    "admitted": (_INPUTS, (_spend(_ABS, "400"), _spend(_PCT, "40", "1000")), None),
    "missing input": ((), (_spend(_ABS, "400"),), PredicateQuietReason.MISSING_INPUT),
    "missing kind": (_INPUTS, (_spend(_ABS, "400"),), PredicateQuietReason.MISSING_KIND),
    "near-zero prior": (
        _INPUTS,
        (_spend(_ABS, "400"), _spend(_PCT, "40", "4")),
        PredicateQuietReason.NEAR_ZERO_PRIOR,
    ),
    "below threshold": (
        _INPUTS,
        (_spend(_ABS, "299"), _spend(_PCT, "40", "1000")),
        PredicateQuietReason.BELOW_THRESHOLD,
    ),
    # A percentage both off a near-zero prior and below its row is below threshold: the
    # bound is not the only thing that removed it.
    "below and near zero": (
        _INPUTS,
        (_spend(_ABS, "400"), _spend(_PCT, "4", "4")),
        PredicateQuietReason.BELOW_THRESHOLD,
    ),
}


@pytest.mark.parametrize("name", list(_CASES))
def test_each_quiet_reason_is_triggered_by_its_own_evidence(name: str) -> None:
    inputs, findings, expected = _CASES[name]
    admission = predicate_admission(
        CONTRACTS[InsightSlotId.CONTRIBUTION], InsightEvidenceBundle(inputs, findings), _TACTICAL
    )
    assert admission.quiet_reason is expected
    assert bool(admission.findings) is (expected is None)


def test_the_slot_evaluation_carries_the_quiet_reason_on_the_quiet_shape_only() -> None:
    contract = CONTRACTS[InsightSlotId.CONTRIBUTION]
    quiet = evaluate_slot_predicate(
        contract, InsightEvidenceBundle(_INPUTS, (_spend(_ABS, "299"),)), _TACTICAL
    )
    ineligible = evaluate_slot_predicate(contract, InsightEvidenceBundle((), ()), _TACTICAL)
    admitted = evaluate_slot_predicate(
        contract,
        InsightEvidenceBundle(_INPUTS, (_spend(_ABS, "400"), _spend(_PCT, "40", "1000"))),
        _TACTICAL,
    )
    assert quiet.quiet_reason is PredicateQuietReason.MISSING_KIND
    assert ineligible.quiet_reason is None and ineligible.missing_evidence == _INPUTS
    assert admitted.quiet_reason is None and admitted.findings
    with pytest.raises(ValueError, match="quiet reason"):
        SlotPredicateEvaluation((), ())
    with pytest.raises(ValueError, match="quiet reason"):
        SlotPredicateEvaluation((), _INPUTS, PredicateQuietReason.MISSING_INPUT)
