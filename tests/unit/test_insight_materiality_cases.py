# SPDX-License-Identifier: MIT-0
"""AC-11 expected-silence and admission cases, run deterministically in `make verify`.

Each contract declares `materiality_cases` beside its thresholds (design §4.3). A case is
built through the real builder seam (`change_findings`, `predicate_findings`) and judged by
the real selection rule (`finding_is_material`), so a threshold edit that moves a declared
outcome fails here, never in the model calibration run.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from campaign_portfolio_support import (
    campaign_display_signature,
    complete_portfolio,
    portfolio_row,
    portfolio_totals,
)

from adapters.campaign_insight_requests import (
    FindingSource,
    change_findings,
    change_priors,
    portfolio_insight_request_derivation,
    predicate_findings,
)
from adapters.jobs.contract_validation.insight_loader import (
    MaterialityCase,
    MaterialityExpectation,
    load_insight_contracts,
    load_materiality_cases,
)
from domain.presentation.campaign_attribution import CampaignSelectionCapability
from domain.presentation.insights import (
    DeterministicFinding,
    DeterministicFindingRequest,
    FindingUnit,
    InsightContract,
    InsightFindingKind,
    InsightFindingOperand,
    InsightHorizonRole,
    InsightSlotId,
    PredicateFinding,
    admitted_predicate_findings,
    calculate_deterministic_findings,
    finding_is_material,
    finding_magnitude,
)
from domain.semantics.analytical_resolution import AuthorizedAnalyticalScope
from domain.semantics.identity import CommerceScopeKey
from domain.shared_kernel.values import DateRange

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-076")]
ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = {contract.id: contract for _, contract in load_insight_contracts(ROOT)}
CASES = load_materiality_cases(ROOT)
_OPERAND = "Fictional operand"


def _built(contract: InsightContract, case: MaterialityCase) -> tuple[PredicateFinding, ...]:
    """Build the case's one finding exactly as the slot's builder would."""
    current, prior = case.current, case.prior
    if case.kind is InsightFindingKind.SHARE_OF_TOTAL:
        findings = calculate_deterministic_findings(
            DeterministicFindingRequest(
                case.kind, (InsightFindingOperand(_OPERAND, current),), total=prior
            )
        )
    elif case.kind is InsightFindingKind.RANKED_ABSOLUTE_CHANGE:
        findings = calculate_deterministic_findings(
            DeterministicFindingRequest(
                case.kind, (InsightFindingOperand(_OPERAND, current, prior),)
            )
        )
    else:
        findings = change_findings(((_OPERAND, current, prior),), case.kind)
    priors = (
        change_priors(((_OPERAND, current, prior),))
        if case.kind is InsightFindingKind.PERCENTAGE_CHANGE
        else None
    )
    return predicate_findings(
        findings,
        case.kind,
        *contract.predicate.required_evidence_inputs,
        source=FindingSource(contract.id, case.basis, case.metric),
        priors=priors,
    )


_ALL = [(slot, case) for slot, cases in CASES.items() for case in cases]


def test_every_contract_declares_cases() -> None:
    assert set(CASES) == set(InsightSlotId)
    assert all(CASES.values())


@pytest.mark.parametrize(
    ("slot", "case"), _ALL, ids=[f"{slot.value}: {case.name}" for slot, case in _ALL]
)
def test_declared_materiality_case(slot: InsightSlotId, case: MaterialityCase) -> None:
    contract = CONTRACTS[slot]
    built = _built(contract, case)
    if case.expect is MaterialityExpectation.NOT_BUILT:
        # A percentage off a zero prior is refused at calculation, before Gate-1 (B-15).
        assert built == ()
        return
    (item,) = built
    material = finding_is_material(contract.predicate.materiality, item, case.horizon)
    threshold = contract.predicate.materiality.threshold_for(
        case.horizon, item.basis, unit=item.unit, metric_id=item.metric_id
    )
    if case.expect is MaterialityExpectation.ADMITTED:
        assert material
    elif case.expect is MaterialityExpectation.SILENT:
        # Silent because the change is below its row, not because of the prior bound.
        assert not material
        assert finding_magnitude(item) < threshold
    else:
        # Suppressed: the change clears its row, and only the near-zero prior silences it.
        assert not material
        assert finding_magnitude(item) >= threshold


@pytest.mark.parametrize("slot", list(InsightSlotId))
def test_every_declared_row_admits_at_its_threshold_and_silences_just_below(
    slot: InsightSlotId,
) -> None:
    """Generated boundary cases for every unit and per-metric row, basis and horizon."""
    materiality = CONTRACTS[slot].predicate.materiality
    kind = InsightFindingKind.ABSOLUTE_CHANGE
    for basis, declared in materiality.bases.items():
        rows = [(unit, "fictional_metric", row) for unit, row in declared.per_unit.items()] + [
            (FindingUnit.COUNT, metric, row) for metric, row in declared.per_metric.items()
        ]
        for unit, metric, row in rows:
            for role in InsightHorizonRole:
                threshold = row.for_role(role)
                scale = Decimal(100) if unit is FindingUnit.RATE_PP else Decimal(1)
                for value, expected in (
                    (threshold, True),
                    (threshold - Decimal("0.0001"), False),
                ):
                    item = PredicateFinding(
                        DeterministicFinding(_OPERAND, value / scale),
                        kind,
                        CONTRACTS[slot].predicate.required_evidence_inputs,
                        unit,
                        basis,
                        metric,
                    )
                    assert finding_is_material(materiality, item, role) is expected, (
                        basis,
                        unit,
                        metric,
                        role,
                        value,
                    )


def _divergence(portfolio_roas: tuple[str, str], campaign_roas: tuple[str, str]) -> bool:
    """Whether the divergence slot is admitted on a 7D portfolio (tactical-review)."""
    period = DateRange(date(2026, 8, 13), date(2026, 8, 19))
    row = portfolio_row(
        campaign_display_signature("Fictional Campaign 001"),
        CampaignSelectionCapability("baked:00000", datetime(2027, 1, 1, tzinfo=UTC)),
        period,
        Decimal("2000"),
        prior_spend=Decimal("1000"),
        roas=(Decimal(campaign_roas[0]), Decimal(campaign_roas[1])),
    )
    totals = portfolio_totals(period, roas=(Decimal(portfolio_roas[0]), Decimal(portfolio_roas[1])))
    derivation, _ = portfolio_insight_request_derivation(
        artifact=complete_portfolio(period, (row,), totals=totals),
        scope=AuthorizedAnalyticalScope(frozenset({CommerceScopeKey("commerce-fictional")})),
        contracts=CONTRACTS,
    )
    return bool(
        admitted_predicate_findings(
            CONTRACTS[InsightSlotId.DIVERGENCE],
            derivation.evidence_by_slot[InsightSlotId.DIVERGENCE],
            InsightHorizonRole.TACTICAL_REVIEW,
        )
    )


def test_divergence_judges_each_operand_at_its_own_basis() -> None:
    """R7-4: the portfolio ROAS meets the portfolio row and each campaign its own row."""
    # Portfolio +0.07 (row 0.06, item 28) with one campaign moving -0.16 against it: fills.
    assert _divergence(("3.07", "3.00"), ("2.84", "3.00"))
    # The same portfolio change just below its row is silent, whatever the campaigns do.
    assert not _divergence(("3.05", "3.00"), ("2.84", "3.00"))
    assert not _divergence(("3.05", "3.00"), ("1.00", "3.00"))
    # A campaign moving against it by less than its own row (0.15) does not diverge.
    assert not _divergence(("3.07", "3.00"), ("2.86", "3.00"))
