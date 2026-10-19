# SPDX-License-Identifier: MIT-0
"""Every insight finding's unit, basis and prior scale, and per-contract completeness.

`ALLOWED_FINDING_UNITS` is the static declaration of what each slot's builder emits; the
metric dictionary decides each triple's unit and prior scale; each contract must declare a
threshold and a near-zero bound for every triple of a kind it requires (ADR-076 §5).
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from campaign_detail_support import detail_fixture

from adapters.campaign_insight_requests import (
    FocusInsightDerivationInput,
    focus_insight_request_derivation,
)
from adapters.insight_finding_units import (
    ALLOWED_FINDING_UNITS,
    FINDING_METRIC_DICTIONARY_IDS,
    FINDING_UNIT_UNDECLARED,
    dictionary_id,
    finding_unit,
    metric_scale,
)
from adapters.jobs.contract_validation.insight_loader import (
    check_materiality_completeness,
    load_insight_contracts,
)
from adapters.outbound.metric_dictionary import load_catalog
from domain.data_contracts.contract import ContractError
from domain.presentation.insights import (
    BasisThresholds,
    FindingUnit,
    InsightContract,
    InsightFindingKind,
    InsightHorizonRole,
    InsightMateriality,
    InsightSlotId,
    MetricScale,
    NearZeroPriors,
    OperandBasis,
)
from domain.presentation.metric_focus import (
    CAMPAIGN_DETAIL_FOCUS_METRICS,
    CampaignDetailFocusRequest,
    MetricFocusIdentity,
    campaign_detail_focus_ingredients,
)
from domain.semantics.analytical_resolution import AuthorizedAnalyticalScope
from domain.semantics.identity import CommerceScopeKey
from domain.shared_kernel.errors import InvariantError

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-076")]
ROOT = Path(__file__).resolve().parents[2]
CATALOG = load_catalog(ROOT)
_ABS = InsightFindingKind.ABSOLUTE_CHANGE
_PCT = InsightFindingKind.PERCENTAGE_CHANGE
_CAMPAIGN = OperandBasis.CAMPAIGN_WINDOW


def _contracts() -> dict[InsightSlotId, InsightContract]:
    return {contract.id: contract for _, contract in load_insight_contracts(ROOT)}


def test_every_slot_declares_what_its_builder_emits() -> None:
    assert set(ALLOWED_FINDING_UNITS) == set(InsightSlotId)
    assert all(ALLOWED_FINDING_UNITS.values())


def test_every_declared_metric_is_a_dictionary_metric() -> None:
    metrics = {metric for triples in ALLOWED_FINDING_UNITS.values() for _, metric, _ in triples}
    for metric in metrics:
        assert CATALOG.metric(dictionary_id(metric)) is not None, metric
    # The map owns only ids that differ; an identity entry would be a second home.
    assert all(key != value for key, value in FINDING_METRIC_DICTIONARY_IDS.items())


@pytest.mark.parametrize(
    ("slot", "kind", "metric", "basis", "unit", "scale"),
    [
        (InsightSlotId.CONTRIBUTION, _ABS, "spend", _CAMPAIGN, FindingUnit.CURRENCY, None),
        (
            InsightSlotId.CONTRIBUTION,
            _PCT,
            "spend",
            _CAMPAIGN,
            FindingUnit.PERCENT,
            MetricScale.CURRENCY,
        ),
        (
            InsightSlotId.DIVERGENCE,
            _ABS,
            "portfolio_roas_attributed_14d",
            OperandBasis.PORTFOLIO_WINDOW,
            FindingUnit.MULTIPLIER,
            None,
        ),
        (
            InsightSlotId.EFFICIENCY_DRIFT,
            _PCT,
            "attributed_roas",
            _CAMPAIGN,
            FindingUnit.PERCENT,
            MetricScale.MULTIPLIER,
        ),
        (InsightSlotId.METRIC_FOCUS, _ABS, "ctr", _CAMPAIGN, FindingUnit.RATE_PP, None),
        (InsightSlotId.METRIC_FOCUS, _ABS, "cpc", _CAMPAIGN, FindingUnit.UNIT_PRICE, None),
        (
            InsightSlotId.METRIC_FOCUS,
            _PCT,
            "cpm",
            _CAMPAIGN,
            FindingUnit.PERCENT,
            MetricScale.UNIT_PRICE,
        ),
        (InsightSlotId.METRIC_FOCUS, _ABS, "clicks", _CAMPAIGN, FindingUnit.COUNT, None),
        (
            InsightSlotId.TREND_SHAPE,
            _ABS,
            "purchases",
            OperandBasis.CAMPAIGN_DAY,
            FindingUnit.COUNT,
            None,
        ),
        (
            InsightSlotId.EXPOSURE,
            InsightFindingKind.SHARE_OF_TOTAL,
            "spend",
            _CAMPAIGN,
            FindingUnit.SHARE,
            None,
        ),
        (
            InsightSlotId.RETURNS_PRESSURE,
            _ABS,
            "return_rate",
            OperandBasis.RETAIL_WINDOW,
            FindingUnit.RATE_PP,
            None,
        ),
        (
            InsightSlotId.RETAIL_MOVERS,
            InsightFindingKind.RANKED_ABSOLUTE_CHANGE,
            "retail_revenue_shipped",
            OperandBasis.PRODUCT_WINDOW,
            FindingUnit.CURRENCY,
            None,
        ),
    ],
)
def test_the_dictionary_decides_each_finding_unit_and_prior_scale(  # noqa: PLR0913, PLR0917
    slot: InsightSlotId,
    kind: InsightFindingKind,
    metric: str,
    basis: OperandBasis,
    unit: FindingUnit,
    scale: MetricScale | None,
) -> None:
    assert finding_unit(slot, kind, metric_id=metric, basis=basis, catalog=CATALOG) == (
        unit,
        scale,
    )


def test_an_undeclared_triple_fails_closed() -> None:
    # Positive control: the declared campaign-window spend change resolves.
    finding_unit(InsightSlotId.CONTRIBUTION, _ABS, metric_id="spend", basis=_CAMPAIGN)
    for kind, metric, basis in (
        (_ABS, "spend", OperandBasis.PORTFOLIO_WINDOW),
        (_ABS, "purchases", _CAMPAIGN),
        (InsightFindingKind.SHARE_OF_TOTAL, "spend", _CAMPAIGN),
    ):
        with pytest.raises(InvariantError) as raised:
            finding_unit(InsightSlotId.CONTRIBUTION, kind, metric_id=metric, basis=basis)
        assert raised.value.code == FINDING_UNIT_UNDECLARED


def test_an_undeclared_dictionary_metric_fails_closed() -> None:
    assert metric_scale("ad_spend", CATALOG) is MetricScale.CURRENCY
    with pytest.raises(InvariantError) as raised:
        metric_scale("fictional_metric", CATALOG)
    assert raised.value.code == FINDING_UNIT_UNDECLARED


@pytest.mark.parametrize("slot", list(InsightSlotId))
def test_every_contract_declares_a_threshold_and_bound_for_each_emitted_finding(
    slot: InsightSlotId,
) -> None:
    contract = _contracts()[slot]
    check_materiality_completeness(contract, CATALOG, where=slot.value)
    required = contract.predicate.required_finding_kinds
    materiality = contract.predicate.materiality
    emitted = [triple for triple in ALLOWED_FINDING_UNITS[slot] if triple[0] in required]
    assert emitted, slot
    for kind, metric, basis in emitted:
        unit, scale = finding_unit(slot, kind, metric_id=metric, basis=basis, catalog=CATALOG)
        key = dictionary_id(metric)
        for role in InsightHorizonRole:
            assert materiality.threshold_for(role, basis, unit=unit, metric_id=key) > 0
        if scale is not None:
            assert materiality.prior_bound_for(scale, key) >= 0


def _with_materiality(
    contract: InsightContract, materiality: InsightMateriality
) -> InsightContract:
    return replace(contract, predicate=replace(contract.predicate, materiality=materiality))


def test_completeness_refuses_a_missing_row_basis_bound_or_stray_metric() -> None:
    contracts = _contracts()
    divergence = contracts[InsightSlotId.DIVERGENCE]
    focus = contracts[InsightSlotId.METRIC_FOCUS]
    bases = dict(divergence.predicate.materiality.bases)
    campaign = bases[_CAMPAIGN]
    no_percent = _with_materiality(
        divergence,
        InsightMateriality(
            {
                **bases,
                _CAMPAIGN: BasisThresholds(
                    {
                        unit: row
                        for unit, row in campaign.per_unit.items()
                        if unit is not FindingUnit.PERCENT
                    }
                ),
            },
            divergence.predicate.materiality.near_zero_prior,
        ),
    )
    one_basis = _with_materiality(
        divergence,
        InsightMateriality({_CAMPAIGN: campaign}, divergence.predicate.materiality.near_zero_prior),
    )
    no_multiplier_bound = _with_materiality(
        divergence, InsightMateriality(bases, NearZeroPriors({}))
    )
    focus_bounds = focus.predicate.materiality.near_zero_prior
    next_check = contracts[InsightSlotId.NEXT_CHECK]
    next_check_bounds = next_check.predicate.materiality.near_zero_prior
    no_count_bound = _with_materiality(
        next_check,
        InsightMateriality(
            next_check.predicate.materiality.bases,
            NearZeroPriors(
                {
                    scale: bound
                    for scale, bound in next_check_bounds.per_scale.items()
                    if scale is not MetricScale.COUNT
                }
            ),
        ),
    )
    focus_bases = dict(focus.predicate.materiality.bases)
    portfolio = focus_bases[OperandBasis.PORTFOLIO_WINDOW]
    stray_clicks = _with_materiality(
        focus,
        InsightMateriality(
            {
                **focus_bases,
                OperandBasis.PORTFOLIO_WINDOW: BasisThresholds(
                    portfolio.per_unit,
                    {**portfolio.per_metric, "clicks": portfolio.per_metric["purchases"]},
                ),
            },
            focus_bounds,
        ),
    )
    campaign_focus = focus_bases[_CAMPAIGN]
    no_cpc_row = _with_materiality(
        focus,
        InsightMateriality(
            {
                **focus_bases,
                _CAMPAIGN: BasisThresholds(
                    campaign_focus.per_unit,
                    {k: v for k, v in campaign_focus.per_metric.items() if k != "cpc"},
                ),
            },
            focus_bounds,
        ),
    )

    # Positive control: the committed contracts are complete.
    check_materiality_completeness(divergence, CATALOG, where="divergence")
    check_materiality_completeness(focus, CATALOG, where="metric-focus")
    for broken, match in (
        (no_percent, "no threshold for percentage-change"),
        (one_basis, "the slot emits"),
        (no_multiplier_bound, "no bound for multiplier"),
        (no_count_bound, "no bound for count"),
        (stray_clicks, "clicks is not an amount"),
        (no_cpc_row, "no threshold for absolute-change cpc"),
    ):
        with pytest.raises(ContractError, match=match):
            check_materiality_completeness(broken, CATALOG, where="fictional")


@pytest.mark.parametrize("metric", CAMPAIGN_DETAIL_FOCUS_METRICS)
def test_every_detail_focus_metric_builds_declared_findings(metric: str) -> None:
    """The campaign-window focus builder branch for each detail tile, through the derivation."""
    detail, delivery = detail_fixture()
    ingredients = campaign_detail_focus_ingredients(
        CampaignDetailFocusRequest(detail, delivery, metric)
    )
    identity = MetricFocusIdentity(
        metric,
        "Fictional label",
        "A fictional definition.",
        ingredients.current.units,
        ingredients.current.basis,
        ingredients.current.grain,
    )
    derivation = focus_insight_request_derivation(
        FocusInsightDerivationInput(
            ingredients=ingredients,
            identity=identity,
            subject=f"campaign-fictional:metric-focus:{metric}",
            scope=AuthorizedAnalyticalScope(frozenset({CommerceScopeKey("commerce-fictional")})),
            artifact_revision="sha256:fictional",
            slot_id=InsightSlotId.METRIC_FOCUS,
            contracts=_contracts(),
            basis=_CAMPAIGN,
        )
    )
    evidence = derivation.evidence_by_slot[InsightSlotId.METRIC_FOCUS]
    assert evidence.findings
    assert {item.metric_id for item in evidence.findings if item.kind is _ABS} == {
        dictionary_id(metric)
    }
    assert all(item.basis is _CAMPAIGN for item in evidence.findings if item.kind is _ABS)
