# SPDX-License-Identifier: MIT-0
"""Generator checks over the declared shape (design §3.3, §3.7 test 3): fail generation, never pass.

Each check raises `SyntheticCatalogError` naming the entity, the window and the value. The bands
are fixed at review; a breach is fixed by a recorded seed change or a narrowed source band, never
by widening a check band.
"""

from __future__ import annotations

import decimal
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from adapters.jobs.fixture_generation.catalog import (
    SyntheticCampaign,
    SyntheticCatalogError,
    SyntheticEntityCatalog,
)
from adapters.jobs.fixture_generation.values import CampaignDayValues

_WINDOWS = (1, 7, 14)
_MIN_CLICKS_FOR_DAILY_CVR = 20
_TIE_GROUP = 3
_CENT = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class TieReport:
    """Coincidences the tie check reports but does not gate (design §3.7 test 3)."""

    identical_integer_pairs: dict[str, int]
    identical_integer_changes: dict[str, int]
    exact_zero_integer_changes: dict[str, int]


def _days(first: date, last: date) -> list[date]:
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]


def check_generated_shape(catalog: SyntheticEntityCatalog) -> TieReport:
    """Run every generator check over the visible catalog's span; return the tie report."""
    with decimal.localcontext(decimal.Context(prec=28)):
        _check_campaigns(catalog)
        _check_retail(catalog)
        return check_ties(catalog, catalog.shape.fixture_span.last_day)


def _check_campaigns(catalog: SyntheticEntityCatalog) -> None:
    shape = catalog.shape
    values = catalog.values
    span = _days(shape.fixture_span.first_day, shape.fixture_span.last_day)
    checks = shape.campaign_checks
    for campaign in catalog.campaigns:
        series = [values.campaign_day(campaign.key, campaign.scenario, day) for day in span]
        for day, item in zip(span, series, strict=True):
            if not item.delivering:
                continue
            if item.trend < shape.min_trend_multiplier:
                raise SyntheticCatalogError(
                    f"{campaign.key} {day}: trend {item.trend} is below min_trend_multiplier."
                )
            if item.purchases > item.clicks:
                raise SyntheticCatalogError(f"{campaign.key} {day}: purchases exceed clicks.")
            if (
                item.clicks >= _MIN_CLICKS_FOR_DAILY_CVR
                and Decimal(item.purchases) / item.clicks > checks.daily_cvr_max
            ):
                raise SyntheticCatalogError(
                    f"{campaign.key} {day}: purchases / clicks exceeds daily_cvr_max."
                )
        if campaign.scenario == "zero_conversion":
            continue
        for end in range(6, len(span)):
            window = series[end - 6 : end + 1]
            clicks = sum(item.clicks for item in window)
            spend = sum((item.spend for item in window), Decimal(0))
            if clicks == 0 or spend == 0:
                continue
            cvr = Decimal(sum(item.purchases for item in window)) / clicks
            roas = sum((item.revenue for item in window), Decimal(0)) / spend
            label = f"{campaign.key} 7D ending {span[end]}"
            if not checks.window_cvr_band.low <= cvr <= checks.window_cvr_band.high:
                raise SyntheticCatalogError(f"{label}: purchases / clicks {cvr:.4f} out of band.")
            if not checks.window_roas_band.low <= roas <= checks.window_roas_band.high:
                raise SyntheticCatalogError(f"{label}: ROAS {roas:.4f} out of band.")


def _check_retail(catalog: SyntheticEntityCatalog) -> None:
    shape = catalog.shape
    values = catalog.values
    retail = shape.retail
    span = _days(shape.fixture_span.first_day, shape.fixture_span.last_day)
    totals_units = [0] * len(span)
    totals_returns = [0] * len(span)
    for product in catalog.products:
        series = [values.product_day(product.asin, product.scenario, day) for day in span]
        for index, (day, item) in enumerate(zip(span, series, strict=True)):
            label = f"{product.asin} {day}"
            if item.returns >= item.units_shipped:
                raise SyntheticCatalogError(f"{label}: returns reach units shipped.")
            net = item.revenue_per_unit / values.price(product.asin, day)
            if not (
                retail.revenue_per_unit.low * Decimal("0.99")
                <= net
                <= retail.revenue_per_unit.high * Decimal("1.01")
            ):
                raise SyntheticCatalogError(f"{label}: net revenue per unit {net:.4f} out of band.")
            served = item.shipped_revenue / item.units_shipped
            if not retail.served_rpu_band.low <= served <= retail.served_rpu_band.high:
                raise SyntheticCatalogError(
                    f"{label}: served revenue per unit {served:.4f} out of served_rpu_band."
                )
            totals_units[index] += item.units_shipped
            totals_returns[index] += item.returns
        _check_windows(
            product.asin,
            [item.units_shipped for item in series],
            [item.returns for item in series],
            span,
        )
    _check_windows("all products", totals_units, totals_returns, span)


def _check_windows(label: str, units: list[int], returns: list[int], span: list[date]) -> None:  # noqa: PLR0913, PLR0917
    for width in (7, 14):
        for end in range(width - 1, len(span)):
            if sum(returns[end - width + 1 : end + 1]) > sum(units[end - width + 1 : end + 1]):
                raise SyntheticCatalogError(
                    f"{label} {width}D ending {span[end]}: returns exceed units shipped."
                )


def _window_total(
    series: dict[date, CampaignDayValues], first: date, last: date
) -> tuple[int, int, int, Decimal, Decimal]:
    days = _days(first, last)
    return (
        sum(series[day].traffic for day in days),
        sum(series[day].clicks for day in days),
        sum(series[day].purchases for day in days),
        sum((series[day].spend for day in days), Decimal(0)),
        sum((series[day].revenue for day in days), Decimal(0)),
    )


def check_ties(catalog: SyntheticEntityCatalog, last_day: date) -> TieReport:
    """Refuse the W2/W7 tie artifacts on the 1D, 7D and 14D windows ending on `last_day`."""
    values = catalog.values
    reported_pairs: Counter[str] = Counter()
    reported_changes: Counter[str] = Counter()
    reported_zero: Counter[str] = Counter()
    for width in _WINDOWS:
        current_first = last_day - timedelta(days=width - 1)
        prior_last = current_first - timedelta(days=1)
        prior_first = prior_last - timedelta(days=width - 1)
        rows: list[
            tuple[
                SyntheticCampaign,
                tuple[int, int, int, Decimal, Decimal],
                tuple[int, int, int, Decimal, Decimal],
            ]
        ] = []
        for campaign in catalog.campaigns:
            series = {
                day: values.campaign_day(campaign.key, campaign.scenario, day)
                for day in _days(prior_first, last_day)
            }
            rows.append(
                (
                    campaign,
                    _window_total(series, current_first, last_day),
                    _window_total(series, prior_first, prior_last),
                )
            )
        _gate_ties(rows, width)
        for index, metric in enumerate(("impressions", "clicks", "purchases")):
            pairs = Counter((current[index], prior[index]) for _, current, prior in rows)
            changes = Counter(current[index] - prior[index] for _, current, prior in rows)
            reported_pairs[f"{width}D {metric}"] += sum(
                n for n in pairs.values() if n >= _TIE_GROUP
            )
            reported_changes[f"{width}D {metric}"] += sum(
                n for n in changes.values() if n >= _TIE_GROUP
            )
            reported_zero[f"{width}D {metric}"] += changes.get(0, 0)
    return TieReport(dict(reported_pairs), dict(reported_changes), dict(reported_zero))


def _gate_ties(
    rows: list[
        tuple[
            SyntheticCampaign,
            tuple[int, int, int, Decimal, Decimal],
            tuple[int, int, int, Decimal, Decimal],
        ]
    ],
    width: int,
) -> None:
    currency: dict[str, Counter[Decimal]] = {"spend": Counter(), "revenue": Counter()}
    ratio: dict[str, Counter[Decimal]] = {"roas": Counter(), "ctr": Counter()}
    trajectories: Counter[tuple[int, ...]] = Counter()
    for campaign, current, prior in rows:
        spend_change = (current[3] - prior[3]).quantize(_CENT)
        revenue_change = (current[4] - prior[4]).quantize(_CENT)
        roas_change = _ratio(current[4], current[3]) - _ratio(prior[4], prior[3])
        ctr_change = _ratio(Decimal(current[1]), Decimal(current[0])) - _ratio(
            Decimal(prior[1]), Decimal(prior[0])
        )
        both_zero = current[0] == 0 and prior[0] == 0
        zero_revenue = campaign.scenario == "zero_conversion"
        for name, change in (("spend", spend_change), ("revenue", revenue_change)):
            if change != 0:
                currency[name][change] += 1
            elif not both_zero and not (name == "revenue" and zero_revenue):
                raise SyntheticCatalogError(
                    f"{campaign.key} {width}D: exact-zero {name} change outside a declared "
                    "zero state."
                )
        if roas_change != 0:
            ratio["roas"][roas_change] += 1
        elif not both_zero and not zero_revenue:
            raise SyntheticCatalogError(
                f"{campaign.key} {width}D: exact-zero ROAS change outside a declared zero state."
            )
        if ctr_change != 0:
            ratio["ctr"][ctr_change] += 1
        if any(current[:3]) or any(prior[:3]):
            trajectories[(*current[:3], *prior[:3])] += 1
    for name, counter in (*currency.items(), *ratio.items()):
        tied = [value for value, count in counter.items() if count >= _TIE_GROUP]
        if tied:
            raise SyntheticCatalogError(
                f"{width}D {name}: {_TIE_GROUP} or more campaigns share the change {tied[0]}."
            )
    if any(count >= _TIE_GROUP for count in trajectories.values()):
        raise SyntheticCatalogError(
            f"{width}D: {_TIE_GROUP} or more campaigns share one count trajectory."
        )


def _ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    return Decimal(0) if denominator == 0 else numerator / denominator
