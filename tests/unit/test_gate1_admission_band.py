# SPDX-License-Identifier: MIT-0
"""Local Gate-1 admission band over the committed fixtures (design 4.3; R7-4, R8-4).

A single aggregate operand (the portfolio's ROAS, a retail total) is alone on its preset, so a
pooled admission rate hides a row that is quiet on every preset by construction. Each such
row is checked across time instead: for every window end in ``last_day - 55 .. last_day`` and
every preset, the operand is computed from the fixture's daily totals and judged against the
contract's own threshold for that preset's horizon role. The share of ends that clear it must
lie in 20-80 %. The rolling band guards later edits only (R8-4): the values were set from the
pooled medians this module prints (``python tests/unit/test_gate1_admission_band.py``).

This is a test double of the lake aggregation over whole-portfolio and whole-retail totals. A
run-date check ties it to the publisher's renderer and the portfolio derivation, so the
double and the served artifact agree. The live B1 Gate-1 report stays the real check.

Every aggregate prior here is far above every declared near-zero bound (asserted), so the
bound cannot suppress a percentage operand and is not modelled.
"""

from __future__ import annotations

import functools
import json
import statistics
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fixture_artifacts_support import fact_rows, fixture_catalog, render_campaign_artifacts

from adapters.jobs.contract_validation.insight_loader import load_insight_contracts
from domain.presentation.campaign_portfolio import CampaignPortfolioMetric
from domain.presentation.insights import (
    FindingUnit,
    InsightHorizonRole,
    InsightSlotId,
    OperandBasis,
    horizon_role_for,
)
from domain.shared_kernel.values import DateRange

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-076", "ADR-086")]
ROOT = Path(__file__).resolve().parents[2]
GENERATED = ROOT / "tests" / "fixtures" / "generated"
PRESETS = (1, 7, 14, 30)
ENDS = 56
BAND = (Decimal(20), Decimal(80))
CONTRACTS = {contract.id: contract for _, contract in load_insight_contracts(ROOT)}
Totals = Mapping[str, Decimal]
_ROAS_ID = CampaignPortfolioMetric.PORTFOLIO_ROAS.value


@functools.cache
def _last_day() -> date:
    return fixture_catalog().shape.fixture_span.last_day


@functools.cache
def _campaign_daily() -> dict[date, dict[str, Decimal]]:
    daily: dict[date, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for fact, measures in (
        ("fct_ad_exposure", ("impressions", "clicks", "spend")),
        ("fct_attributed_conversion", ("purchases", "revenue_attributed")),
    ):
        for row in fact_rows(fact):
            day = daily[row["date_key"]]  # type: ignore[index]
            for measure in measures:
                day[measure] += row.get(measure, Decimal(0))  # type: ignore[operator]
    return daily


@functools.cache
def _retail_daily() -> dict[date, dict[str, Decimal]]:
    daily: dict[date, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    sales = json.loads((GENERATED / "vendor-sales-by-asin.json").read_text())["salesByAsin"]
    for row in sales:
        day = daily[date.fromisoformat(row["startDate"])]
        day["retail_revenue_shipped"] += Decimal(str(row["shippedRevenue"]["amount"]))
        day["cogs_shipped"] += Decimal(str(row["shippedCogs"]["amount"]))
        day["revenue_ordered"] += Decimal(str(row["orderedRevenue"]["amount"]))
        day["units_shipped"] += row["shippedUnits"]
        day["customer_returns"] += row["customerReturns"]
    traffic = json.loads((GENERATED / "vendor-traffic-by-asin.json").read_text())["trafficByAsin"]
    for row in traffic:
        daily[date.fromisoformat(row["startDate"])]["glance_views"] += row["glanceViews"]
    return daily


def _window(daily: Mapping[date, Mapping[str, Decimal]], window: DateRange) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for offset in range(window.days):
        for name, value in daily[window.start + timedelta(days=offset)].items():
            totals[name] += value
    return totals


def _portfolio(totals: Totals) -> dict[str, Decimal]:
    return {
        "spend": totals["spend"],
        "purchases": totals["purchases"],
        "impressions": totals["impressions"],
        _ROAS_ID: totals["revenue_attributed"] / totals["spend"],
    }


def _retail(totals: Totals) -> dict[str, Decimal]:
    return {**totals, "return_rate": totals["customer_returns"] / totals["units_shipped"]}


@dataclass(frozen=True, slots=True)
class BandRow:
    """One aggregate operand row: its slot, basis, unit and the metrics it pools."""

    slot: InsightSlotId
    basis: OperandBasis
    unit: FindingUnit
    metrics: tuple[str, ...]
    values: Callable[[Totals], dict[str, Decimal]]
    daily: Callable[[], dict[date, dict[str, Decimal]]]

    @property
    def name(self) -> str:
        return f"{self.slot.value} {self.basis.value} {self.unit.value}"


_ROAS = (_ROAS_ID,)
_RETAIL_FOCUS_PERCENT = (
    "retail_revenue_shipped",
    "units_shipped",
    "customer_returns",
    "glance_views",
    "revenue_ordered",
    "cogs_shipped",
    "return_rate",
)
ROWS = (
    # Divergence's gating operand: the portfolio ROAS change (one per preset and segment; the
    # whole-portfolio total is the same on every segment).
    BandRow(
        InsightSlotId.DIVERGENCE,
        OperandBasis.PORTFOLIO_WINDOW,
        FindingUnit.MULTIPLIER,
        _ROAS,
        _portfolio,
        _campaign_daily,
    ),
    BandRow(
        InsightSlotId.DIVERGENCE,
        OperandBasis.PORTFOLIO_WINDOW,
        FindingUnit.PERCENT,
        _ROAS,
        _portfolio,
        _campaign_daily,
    ),
    BandRow(
        InsightSlotId.METRIC_FOCUS,
        OperandBasis.PORTFOLIO_WINDOW,
        FindingUnit.MULTIPLIER,
        _ROAS,
        _portfolio,
        _campaign_daily,
    ),
    BandRow(
        InsightSlotId.METRIC_FOCUS,
        OperandBasis.PORTFOLIO_WINDOW,
        FindingUnit.PERCENT,
        ("spend", "purchases", "impressions", *_ROAS),
        _portfolio,
        _campaign_daily,
    ),
    BandRow(
        InsightSlotId.RETAIL_METRIC_FOCUS,
        OperandBasis.RETAIL_WINDOW,
        FindingUnit.RATE_PP,
        ("return_rate",),
        _retail,
        _retail_daily,
    ),
    BandRow(
        InsightSlotId.RETAIL_METRIC_FOCUS,
        OperandBasis.RETAIL_WINDOW,
        FindingUnit.PERCENT,
        _RETAIL_FOCUS_PERCENT,
        _retail,
        _retail_daily,
    ),
    BandRow(
        InsightSlotId.RETURNS_PRESSURE,
        OperandBasis.RETAIL_WINDOW,
        FindingUnit.RATE_PP,
        ("return_rate",),
        _retail,
        _retail_daily,
    ),
    BandRow(
        InsightSlotId.RETURNS_PRESSURE,
        OperandBasis.RETAIL_WINDOW,
        FindingUnit.PERCENT,
        ("customer_returns", "return_rate"),
        _retail,
        _retail_daily,
    ),
    BandRow(
        InsightSlotId.DEMAND_SHAPE,
        OperandBasis.RETAIL_WINDOW,
        FindingUnit.PERCENT,
        ("retail_revenue_shipped", "units_shipped", "glance_views"),
        _retail,
        _retail_daily,
    ),
)
"""Ordered-vs-shipped compares ordered with shipped in one window, so it is reported only."""


def _windows(days: int, end: date) -> tuple[DateRange, DateRange]:
    current = DateRange(end - timedelta(days=days - 1), end)
    prior_end = current.start - timedelta(days=1)
    return current, DateRange(prior_end - timedelta(days=days - 1), prior_end)


def magnitudes(row: BandRow, days: int) -> list[Decimal]:
    """Every pooled operand magnitude over the 56 window ends, in the threshold's own unit."""
    daily = row.daily()
    found: list[Decimal] = []
    for back in range(ENDS):
        current, prior = _windows(days, _last_day() - timedelta(days=back))
        now, before = row.values(_window(daily, current)), row.values(_window(daily, prior))
        for metric in row.metrics:
            assert before[metric] > 0, f"{row.name} {metric}: a zero aggregate prior"
            change = now[metric] - before[metric]
            if row.unit is FindingUnit.PERCENT:
                found.append(abs(change / before[metric] * 100))
            elif row.unit is FindingUnit.RATE_PP:
                found.append(abs(change) * 100)
            else:
                found.append(abs(change))
    return found


def _threshold(row: BandRow, days: int) -> Decimal:
    materiality = CONTRACTS[row.slot].predicate.materiality
    return materiality.threshold_for(
        horizon_role_for(days), row.basis, unit=row.unit, metric_id=row.metrics[0]
    )


def admission(row: BandRow, days: int) -> Decimal:
    values = magnitudes(row, days)
    threshold = _threshold(row, days)
    return Decimal(sum(value >= threshold for value in values) * 100) / len(values)


@pytest.mark.parametrize("days", PRESETS)
@pytest.mark.parametrize("row", ROWS, ids=lambda row: row.name)
def test_every_aggregate_operand_row_admits_within_the_band(row: BandRow, days: int) -> None:
    share = admission(row, days)
    assert BAND[0] <= share <= BAND[1], (
        f"{row.name} at {days}D admits {share:.1f}% of {ENDS} window ends against "
        f"{_threshold(row, days)}; fix the contract value with the table in the commit body."
    )


def test_the_band_check_catches_a_threshold_no_window_clears() -> None:
    """Positive control: the previous percent rows (5 % at 7D) admitted no window end."""
    row = next(item for item in ROWS if item.slot is InsightSlotId.DEMAND_SHAPE)
    values = magnitudes(row, 7)
    assert sum(value >= Decimal(5) for value in values) * 100 / len(values) < BAND[0]


def test_the_double_matches_the_served_portfolio_roas_at_the_run_date() -> None:
    """The rolling double and the publisher's renderer agree on the gating operand."""
    current, prior = _windows(7, _last_day())
    rendered = render_campaign_artifacts(current, prior, channel=None)
    assert rendered is not None
    total = next(
        item
        for item in rendered.portfolio.totals
        if item.metric is CampaignPortfolioMetric.PORTFOLIO_ROAS
    )
    daily = _campaign_daily()
    now, before = _portfolio(_window(daily, current)), _portfolio(_window(daily, prior))
    # The served ratio is quantized to four places; the double agrees at that precision.
    quantum = Decimal("0.0001")
    assert Decimal(str(total.current.value)) == now[_ROAS_ID].quantize(quantum)
    assert Decimal(str(total.prior.value)) == before[_ROAS_ID].quantize(quantum)


def calibration_table() -> list[str]:
    """Pooled medians per horizon role, and each preset's admission at the declared value."""
    lines = [f"fixture last_day {_last_day()}; {ENDS} window ends per preset"]
    for row in ROWS:
        by_role: dict[InsightHorizonRole, list[Decimal]] = defaultdict(list)
        for days in PRESETS:
            by_role[horizon_role_for(days)].extend(magnitudes(row, days))
        medians = {
            role.value: f"{statistics.median(values):.3f}" for role, values in by_role.items()
        }
        shares = {f"{days}D": f"{admission(row, days):.0f}%" for days in PRESETS}
        lines.append(f"{row.name}: medians {medians}; admission {shares}")
    return lines


if __name__ == "__main__":
    print("\n".join(calibration_table()))  # noqa: T201
