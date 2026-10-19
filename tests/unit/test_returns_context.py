# SPDX-License-Identifier: MIT-0
"""C4, F16 and RL-2: what the returns card and campaign deep analysis are told.

F16: the return-rate narration read a raw ratio (1.4364) as a percentage ("1.4 %") beside a
tile showing 143.64 %. The card context now states every rate in its tile's unit.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from campaign_portfolio_support import (
    campaign_display_signature,
    complete_portfolio,
    portfolio_row,
)
from change_support import SEMANTICS

from adapters.campaign_insight_requests import portfolio_insight_request_derivation
from adapters.deep_insight_inputs import attribution_timing_entry, portfolio_deep_inputs
from adapters.jobs.contract_validation.insight_loader import load_insight_contracts
from adapters.outbound.metric_dictionary import load_catalog
from adapters.retail_insight_requests import (
    RETURNS_ESTIMATE_DEFINITION,
    RetailInsightRequestDerivationInput,
    retail_insight_request_derivation,
)
from domain.narrative.shaped_response import UnitKind, Units
from domain.presentation.campaign_attribution import CampaignSelectionCapability
from domain.presentation.insights import (
    InsightContextEntry,
    InsightSlotId,
    unavailable_insight_slots,
)
from domain.presentation.retail_health import (
    RETAIL_HEALTH_INSIGHT_IDS,
    RETAIL_HEALTH_METRIC_UNITS,
    RETAIL_HEALTH_ROW_GRAIN,
    RETAIL_HEALTH_ROW_METRICS,
    RETAIL_HEALTH_TOTAL_GRAIN,
    RETAIL_HEALTH_TOTAL_METRICS,
    RETAIL_HEALTH_TREND_GRAIN,
    RETAIL_HEALTH_TREND_METRICS,
    RetailComparison,
    RetailDistributorView,
    RetailHealthArtifact,
    RetailHealthOutcome,
    RetailMetricValue,
    RetailProductRow,
    RetailReconciliation,
    RetailTrendPoint,
    retail_comparison,
)
from domain.semantics.analytical_resolution import AuthorizedAnalyticalScope
from domain.semantics.identity import CommerceScopeKey
from domain.shared_kernel.values import DateRange

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-076", "ADR-086")]
ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = {contract.id: contract for _, contract in load_insight_contracts(ROOT)}
CATALOG = load_catalog(ROOT)
PERIOD = DateRange(date(2026, 9, 29), date(2026, 10, 5))
PRIOR = DateRange(date(2026, 9, 22), date(2026, 9, 28))
SCOPE = AuthorizedAnalyticalScope(frozenset({CommerceScopeKey("commerce-fictional")}))

# The F16 capture's values: 9,858 returns on 6,863 shipped is 143.64 % (prior 360.00 %).
_VALUES: dict[str, tuple[str, str]] = {
    "return_rate": ("1.4364", "3.6000"),
    "customer_returns": ("9858", "9000"),
    "units_shipped": ("6863", "2500"),
    "retail_revenue_shipped": ("41818.80", "29000.00"),
}


def _units(metric: str) -> Units:
    kind = RETAIL_HEALTH_METRIC_UNITS[metric]
    return Units(kind, "USD" if kind is UnitKind.CURRENCY else None)


def _comparison(metric: str, current: str, prior: str) -> RetailComparison:
    units = _units(metric)
    # Every comparison carries its governed change block (ADR-121).
    return retail_comparison(
        metric,
        RetailMetricValue(current, units, PERIOD, RETAIL_HEALTH_TOTAL_GRAIN),
        RetailMetricValue(prior, units, PRIOR, RETAIL_HEALTH_TOTAL_GRAIN),
        SEMANTICS,
    )


def _trend() -> tuple[RetailTrendPoint, ...]:
    def values(day: date, figure: int) -> tuple[RetailMetricValue, ...]:
        return tuple(
            RetailMetricValue(
                figure, _units(metric), DateRange(day, day), RETAIL_HEALTH_TREND_GRAIN
            )
            for metric in RETAIL_HEALTH_TREND_METRICS
        )

    return tuple(
        RetailTrendPoint(
            PERIOD.start + timedelta(days=offset),
            values(PERIOD.start + timedelta(days=offset), 2),
            values(PRIOR.start + timedelta(days=offset), 1),
        )
        for offset in range(PERIOD.days)
    )


_PRODUCT_RETURNS = ((40, 10), (5, 25), (12, 12))
"""Fictional current and prior customer returns for three products (one against the total)."""


def _product_rows() -> tuple[RetailProductRow, ...]:
    def comparison(metric: str, current: int, prior: int) -> RetailComparison:
        units = _units(metric)
        values = ("0.10", "0.05") if metric == "return_rate" else (str(current), str(prior))
        return retail_comparison(
            metric,
            RetailMetricValue(values[0], units, PERIOD, RETAIL_HEALTH_ROW_GRAIN),
            RetailMetricValue(values[1], units, PRIOR, RETAIL_HEALTH_ROW_GRAIN),
            SEMANTICS,
        )

    return tuple(
        RetailProductRow(
            f"B0FICT{index:04d}",
            "B0FICTPARENT",
            f"Fictional Product {index}",
            tuple(
                comparison(metric, *(returns if metric == "customer_returns" else (100, 90)))
                for metric in RETAIL_HEALTH_ROW_METRICS
            ),
            (),
            ("Latest day is preliminary",) if index == 0 else (),
        )
        for index, returns in enumerate(_PRODUCT_RETURNS)
    )


def _artifact(rows: tuple[RetailProductRow, ...] = ()) -> RetailHealthArtifact:
    totals = tuple(
        _comparison(
            metric,
            *_VALUES.get(
                metric,
                ("0.2000", "0.1000")
                if RETAIL_HEALTH_METRIC_UNITS[metric] is UnitKind.RATIO
                else ("200", "100"),
            ),
        )
        for metric in RETAIL_HEALTH_TOTAL_METRICS
    )
    return RetailHealthArtifact(
        outcome=RetailHealthOutcome.COMPLETE,
        period=PERIOD,
        prior_period=PRIOR,
        distributor_view=RetailDistributorView.MANUFACTURING,
        totals=totals,
        absences=(),
        insights=unavailable_insight_slots(RETAIL_HEALTH_INSIGHT_IDS),
        trend=_trend(),
        rows=rows,
        total_count=len(rows),
        truncation=None,
        reconciliation=RetailReconciliation("reconciled", PERIOD.end),
        boundary="Fictional retail boundary statement.",
    )


def _returns_context(rows: tuple[RetailProductRow, ...] = ()) -> dict[str, str]:
    derivation = retail_insight_request_derivation(
        RetailInsightRequestDerivationInput(
            artifact=_artifact(rows), scope=SCOPE, contracts=CONTRACTS, catalog=CATALOG
        )
    )
    request = next(
        item for item in derivation.requests if item.key.slot_id is InsightSlotId.RETURNS_PRESSURE
    )
    return {entry.name: entry.value for entry in request.context}


def test_f16_a_rate_reaches_the_card_in_its_tiles_unit() -> None:
    context = _returns_context()
    assert context["Return rate (current vs prior)"] == (
        "143.64% vs 360.00% (-216.36 percentage points)"
    )
    # Positive control: the raw ratio the model misread is gone from the card input.
    assert all("1.4364" not in value for value in context.values())


def test_c4_states_the_estimate_definition_and_its_operands() -> None:
    context = _returns_context()
    assert context["returns estimate definition"] == RETURNS_ESTIMATE_DEFINITION
    assert context["Units shipped (current vs prior)"] == "6863 vs 2500"
    assert (
        context["Retail revenue (shipped) (current vs prior)"] == "41,818.80 USD vs 29,000.00 USD"
    )
    assert context["shipped revenue per unit (current vs prior)"] == "6.09 USD vs 11.60 USD"
    # No signed returned-minus-shipped gap is added (returns-level-v3 is on HOLD).
    assert not any("gap" in name for name in context)


def test_rl1_supplies_no_returns_timing_entry() -> None:
    """RL-1 = No (FR-3.4 UNMET): no first-report signal exists, so nothing claims one."""
    assert "returns timing" not in _returns_context()


def test_rl2_names_the_window_days_attribution_may_still_credit() -> None:
    entry = attribution_timing_entry(PERIOD)
    assert entry == InsightContextEntry(
        "attribution timing",
        "Attributed purchases for 2026-09-29 to 2026-10-05 are still being credited; later "
        "reports usually add purchases.",
    )
    # The governed window is the contract's 30 days: days older than it are final.
    narrowed = attribution_timing_entry(PERIOD, as_of=date(2026, 10, 30), restatement_days=30)
    assert narrowed is not None and "2026-10-01 to 2026-10-05" in narrowed.value
    assert attribution_timing_entry(PERIOD, as_of=date(2026, 12, 1), restatement_days=30) is None


def test_rl2_reaches_campaign_deep_only() -> None:
    """Not discriminating on cards (every served window is inside 30 days): deep only."""
    rows = tuple(
        portfolio_row(
            campaign_display_signature(f"Fictional Campaign {index}"),
            CampaignSelectionCapability(f"baked:{index:05d}", datetime(2027, 1, 1, tzinfo=UTC)),
            PERIOD,
            Decimal(2000 + index),
            prior_spend=Decimal(1000),
        )
        for index in range(3)
    )
    artifact = complete_portfolio(PERIOD, rows)
    derivation, _ = portfolio_insight_request_derivation(
        artifact=artifact, scope=SCOPE, contracts=CONTRACTS
    )
    assert derivation.requests
    for request in derivation.requests:
        assert all(entry.name != "attribution timing" for entry in request.context)
    deep = portfolio_deep_inputs(artifact, InsightSlotId.CONTRIBUTION, CATALOG)
    assert "attribution timing" in {entry.name for entry in deep.context}


def test_fr11_the_returns_card_lists_every_product_with_the_dialog_summary() -> None:
    """R6-5: all N products, so no listed-to-total reading can leave one out."""
    context = _returns_context(_product_rows())

    assert context["driver scope"] == "Largest changes among all 3 products in this view"
    drivers = context["largest drivers"]
    assert drivers.index("Fictional Product 0") < drivers.index("Fictional Product 1")
    assert "Fictional Product 1 (B0FICT0001): -20 (Moved against the overall change)" in drivers
    assert "Fictional Product 2 (B0FICT0002): 0" in drivers
    assert context["direction counts in scope"] == "Among these 3: 1 rose, 1 fell, 1 were flat"
    # Every product is listed, so no "other" or "outside" line exists.
    assert not any(name.startswith(("other products", "products outside")) for name in context)
    assert context["data sufficiency"] == (
        "This view's data is not complete: Latest day is preliminary."
    )
    assert context["comparison window"] == "2026-09-22 to 2026-09-28, the 7 days before this view"


def test_fr11_without_product_rows_no_driver_lines_are_supplied() -> None:
    """Positive control for the list: the served rows are its only source."""
    context = _returns_context()
    assert "largest drivers" not in context and "driver scope" not in context
    assert context["data sufficiency"] == "This view's data is complete."
