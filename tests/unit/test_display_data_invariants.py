# SPDX-License-Identifier: MIT-0
"""Owner decision #8 (2026-10-05): fix the data before display, never flag it on the page.

The 2026-10-04 UX review found three data-shape defects on the earlier dev generation:
returns above shipped units (F16), an implausibly small prior retail week (F11), and the same
CTR on every campaign (F24). The shaped seed (ADR-086) removes them; these invariants pin it
over the committed fixtures, and each check is shown to catch the defect it guards.

The 2026-10-22 Scorecard investigation found a fourth: the deduplicated-attribution feed held one
row per day, one model and one channel, rotating, so DSP and Sponsored Brands read 0.00x and the
headline differed by model. The deduplicated shape invariant (dev-seeding §2) pins it here too.
"""

from __future__ import annotations

import csv
import json
import statistics
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
import yaml

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-086")]
ROOT = Path(__file__).resolve().parents[2]
GENERATED = ROOT / "tests" / "fixtures" / "generated"
MIN_DAY_SHARE_OF_MEDIAN = Decimal("0.25")
"""A served day's shipped revenue below a quarter of the span median reads as a data gap."""
MIN_DISTINCT_CTRS = 10


def _vendor_days() -> dict[date, tuple[int, int, Decimal]]:
    """Shipped units, customer returns and shipped revenue per day, over every product."""
    raw = json.loads((GENERATED / "vendor-sales-by-asin.json").read_text())
    days: dict[date, list[Decimal]] = defaultdict(lambda: [Decimal(0)] * 3)
    for row in raw["salesByAsin"]:
        totals = days[date.fromisoformat(row["startDate"])]
        totals[0] += row["shippedUnits"]
        totals[1] += row["customerReturns"]
        totals[2] += Decimal(str(row["shippedRevenue"]["amount"]))
    return {day: (int(v[0]), int(v[1]), v[2]) for day, v in days.items()}


def returns_above_shipped(days: dict[date, tuple[int, int, Decimal]]) -> list[date]:
    """F16: days whose customer returns exceed their shipped units."""
    return sorted(day for day, (units, returns, _) in days.items() if returns > units)


def implausible_revenue_days(days: dict[date, tuple[int, int, Decimal]]) -> list[date]:
    """F11: days whose shipped revenue is under a quarter of the span median."""
    median = Decimal(str(statistics.median(revenue for _, _, revenue in days.values())))
    return sorted(
        day for day, (_, _, revenue) in days.items() if revenue < median * MIN_DAY_SHARE_OF_MEDIAN
    )


def _campaign_ctrs(rows: list[dict[str, str]], end: date) -> list[Decimal]:
    start = end - timedelta(days=6)
    totals: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for row in rows:
        day = date.fromisoformat(row["event_date_utc"])
        if start <= day <= end and row["impressions"]:
            totals[row["campaign_id_string"]][0] += int(row["impressions"])
            totals[row["campaign_id_string"]][1] += int(row["clicks"])
    return [
        (Decimal(clicks) / Decimal(impressions)).quantize(Decimal("0.0001"))
        for impressions, clicks in totals.values()
        if impressions
    ]


def test_f16_no_served_day_returns_more_units_than_it_shipped() -> None:
    days = _vendor_days()
    assert len(days) > 400
    assert returns_above_shipped(days) == []
    # Positive control: the review's capture (9,858 returns on 6,863 shipped) is caught.
    capture = {date(2026, 9, 15): (6863, 9858, Decimal("41818.80"))}
    assert returns_above_shipped(capture) == [date(2026, 9, 15)]


def test_f11_no_served_day_is_an_implausibly_small_prior() -> None:
    days = _vendor_days()
    assert implausible_revenue_days(days) == []
    # Positive control: the review's 294.84 USD prior week among ~20,000 USD days is caught.
    shaped = {date(2026, 9, 1) + timedelta(days=i): (100, 5, Decimal(20000)) for i in range(9)}
    shaped[date(2026, 9, 3)] = (2, 0, Decimal("294.84"))
    assert implausible_revenue_days(shaped) == [date(2026, 9, 3)]


def test_f24_campaign_ctr_varies_across_campaigns() -> None:
    rows = list(csv.DictReader((GENERATED / "amc-reach-frequency.csv").open()))
    end = max(date.fromisoformat(row["event_date_utc"]) for row in rows)
    ctrs = _campaign_ctrs(rows, end)
    assert len(ctrs) >= MIN_DISTINCT_CTRS
    assert len(set(ctrs)) >= MIN_DISTINCT_CTRS
    # Positive control: a constant 0.0832 on every row (the review's capture) has one value.
    constant = [
        {
            "event_date_utc": end.isoformat(),
            "campaign_id_string": f"c{i}",
            "impressions": "10000",
            "clicks": "832",
        }
        for i in range(20)
    ]
    assert len(set(_campaign_ctrs(constant, end))) == 1


# --- Deduplicated attribution shape (dev-seeding §2) -----------------------------------------

DSP = "amazon_dsp"
SB = "sponsored_brands"
WINDOW_DAYS = 7
"""The Scorecard's default window, the one the investigation captured (Sep 30 - Oct 6)."""
type Rows = list[dict[str, str]]


def _registered_models() -> tuple[str, ...]:
    raw = cast(
        "dict[str, list[dict[str, str]]]",
        yaml.safe_load((ROOT / "contracts/shared/attribution-models.yaml").read_text()),
    )
    return tuple(item["key"] for item in raw["models"])


def _deduplicated() -> Rows:
    return list(csv.DictReader((GENERATED / "amc-deduplicated-temporal-attribution.csv").open()))


def _attributed() -> Rows:
    return list(csv.DictReader((GENERATED / "amc-attributed-conversion.csv").open()))


def days_missing_a_model(rows: Rows, models: tuple[str, ...]) -> list[date]:
    """Covered days whose rows don't carry every registered model."""
    found: dict[date, set[str]] = defaultdict(set)
    for row in rows:
        found[date.fromisoformat(row["event_date_utc"])].add(row["attribution_model_key"])
    return sorted(day for day, keys in found.items() if keys != set(models))


def days_with_unequal_model_totals(rows: Rows, models: tuple[str, ...]) -> list[date]:
    """Days whose models credit different purchase or revenue totals (each credits once).

    Every registered model is compared, so a model with no row that day credits zero.
    """
    totals: dict[date, dict[str, list[Decimal]]] = defaultdict(
        lambda: defaultdict(lambda: [Decimal(0), Decimal(0)])
    )
    for row in rows:
        total = totals[date.fromisoformat(row["event_date_utc"])][row["attribution_model_key"]]
        total[0] += Decimal(row["matched_purchase_credit"])
        total[1] += Decimal(row["revenue_attributed_deduplicated_incl_halo"])
    return sorted(
        day
        for day, credited in totals.items()
        if len({tuple(credited[model]) for model in models}) > 1
    )


def _attributed_by_day(attributed: Rows, column: str) -> dict[date, dict[str, Decimal]]:
    found: dict[date, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for row in attributed:
        day = date.fromisoformat(row["conversion_event_date_utc"])
        found[day][row["ad_product_type"]] += Decimal(row[column])
    return found


def _linear_by_day(rows: Rows) -> dict[date, dict[str, tuple[Decimal, Decimal]]]:
    found: dict[date, dict[str, tuple[Decimal, Decimal]]] = defaultdict(dict)
    for row in rows:
        if row["attribution_model_key"] == "linear_v1":
            found[date.fromisoformat(row["event_date_utc"])][row["ad_product_type"]] = (
                Decimal(row["matched_purchase_credit"]),
                Decimal(row["revenue_attributed_deduplicated_incl_halo"]),
            )
    return found


def uncredited_channel_days(rows: Rows, attributed: Rows) -> list[tuple[date, str]]:
    """(day, channel) pairs with attributed purchases that day but no linear credit."""
    purchases = _attributed_by_day(attributed, "purchases")
    linear = _linear_by_day(rows)
    return sorted(
        (day, channel)
        for day, channels in purchases.items()
        for channel, count in channels.items()
        if count > 0 and min(linear.get(day, {}).get(channel, (Decimal(0), Decimal(0)))) <= 0
    )


def window_linear_credit(rows: Rows, start: date, end: date) -> dict[str, Decimal]:
    """Each channel's linear purchase credit summed over [start, end]."""
    found: dict[str, Decimal] = defaultdict(Decimal)
    for day, channels in _linear_by_day(rows).items():
        if start <= day <= end:
            for channel, (credit, _) in channels.items():
                found[channel] += credit
    return found


def days_above_attributed_revenue(rows: Rows, attributed: Rows) -> list[tuple[date, str]]:
    """(day, model) pairs whose deduplicated revenue exceeds the channels' halo-inclusive sum."""
    ceiling = {
        day: sum(channels.values(), Decimal(0))
        for day, channels in _attributed_by_day(attributed, "total_product_sales").items()
    }
    totals: dict[tuple[date, str], Decimal] = defaultdict(Decimal)
    for row in rows:
        key = (date.fromisoformat(row["event_date_utc"]), row["attribution_model_key"])
        totals[key] += Decimal(row["revenue_attributed_deduplicated_incl_halo"])
    return sorted(key for key, total in totals.items() if total > ceiling.get(key[0], Decimal(0)))


def _rotating(days: int) -> Rows:
    """Build the retired shape: one row per day, the whole day on one model and one channel."""
    models = ("linear_v1", "first_touch_v1", "last_touch_v1")
    return [
        {
            "event_date_utc": (date(2026, 9, 30) + timedelta(days=i)).isoformat(),
            "ad_product_type": "sponsored_products",
            "attribution_model_key": models[i % 3],
            "matched_purchase_credit": "100.0000",
            "revenue_attributed_deduplicated_incl_halo": "3000.0000",
        }
        for i in range(days)
    ]


def _attributed_day(day: date, channel: str, measures: tuple[int, str]) -> dict[str, str]:
    """Build one attributed-conversion row from (purchases, halo-inclusive sales)."""
    return {
        "conversion_event_date_utc": day.isoformat(),
        "ad_product_type": channel,
        "purchases": str(measures[0]),
        "total_product_sales": measures[1],
    }


def test_deduplicated_attribution_carries_every_registered_model_on_every_covered_day() -> None:
    rows = _deduplicated()
    models = _registered_models()
    assert len(models) == 3
    assert len({row["event_date_utc"] for row in rows}) > 400
    assert days_missing_a_model(rows, models) == []
    # Positive control: the retired rotation carries one model per day.
    assert days_missing_a_model(_rotating(3), models) == [
        date(2026, 9, 30),
        date(2026, 10, 1),
        date(2026, 10, 2),
    ]


def test_deduplicated_attribution_credits_each_model_the_same_daily_total() -> None:
    models = _registered_models()
    assert days_with_unequal_model_totals(_deduplicated(), models) == []
    # Positive control: one model crediting a day 1.00 more than the others is caught.
    day = [
        {**row, "event_date_utc": "2026-10-01", "attribution_model_key": model}
        for row, model in zip(_rotating(3), models, strict=True)
    ]
    assert days_with_unequal_model_totals(day, models) == []
    day[1]["matched_purchase_credit"] = "101.0000"
    assert days_with_unequal_model_totals(day, models) == [date(2026, 10, 1)]


def test_dsp_and_sponsored_brands_get_linear_credit_in_the_last_seven_days() -> None:
    rows, attributed = _deduplicated(), _attributed()
    end = max(date.fromisoformat(row["event_date_utc"]) for row in rows)
    start = end - timedelta(days=WINDOW_DAYS - 1)
    bought = _attributed_by_day(attributed, "purchases")
    for channel in (DSP, SB):
        # Both channels have attributed purchases in the window, so they have touches to credit.
        assert sum(bought[day][channel] for day in bought if start <= day <= end) > 0, channel
    credit = window_linear_credit(rows, start, end)
    assert (credit[DSP] > 0, credit[SB] > 0) == (True, True)
    assert uncredited_channel_days(rows, attributed) == []
    # Positive control: the rotation's Sponsored Products-only week leaves DSP uncredited.
    day = date(2026, 9, 30)
    assert window_linear_credit(_rotating(7), day, day + timedelta(days=6))[DSP] == 0
    assert uncredited_channel_days(_rotating(1), [_attributed_day(day, DSP, (5, "150.00"))]) == [
        (day, DSP)
    ]


def test_deduplicated_revenue_never_exceeds_the_channels_halo_inclusive_attributed_revenue() -> (
    None
):
    assert days_above_attributed_revenue(_deduplicated(), _attributed()) == []
    # Positive control: a day crediting 3,000.00 against 2,999.99 of attributed sales is caught.
    day = date(2026, 9, 30)
    rows = _rotating(1)
    assert days_above_attributed_revenue(
        rows, [_attributed_day(day, "sponsored_products", (100, "2999.99"))]
    ) == [(day, "linear_v1")]


def credit_above_own_attributed(rows: Rows, attributed: Rows) -> list[tuple[date, str, str]]:
    """(day, model, channel) whose credit exceeds that channel's own attributed measure that day.

    A model credits a channel only for purchases it touched, and the channel's attributed
    purchases and halo-inclusive sales count each of those whole.
    """
    purchases = _attributed_by_day(attributed, "purchases")
    sales = _attributed_by_day(attributed, "total_product_sales")
    found: set[tuple[date, str, str]] = set()
    for row in rows:
        day, channel = date.fromisoformat(row["event_date_utc"]), row["ad_product_type"]
        if (
            Decimal(row["matched_purchase_credit"]) > purchases[day][channel]
            or Decimal(row["revenue_attributed_deduplicated_incl_halo"]) > sales[day][channel]
        ):
            found.add((day, row["attribution_model_key"], channel))
    return sorted(found)


def days_split_by_model_without_overlap(rows: Rows, attributed: Rows) -> list[date]:
    """Days whose models credit a channel different revenue, yet dedup equals the attributed sum.

    A model moves a channel's credit only through a purchase that touched more than one channel,
    which the channels' attributed sum counts once per channel and the deduplicated total once,
    so on such a day the deduplicated revenue sits below the attributed sum.
    """
    ceiling = {
        day: sum(channels.values(), Decimal(0))
        for day, channels in _attributed_by_day(attributed, "total_product_sales").items()
    }
    credit: dict[date, dict[str, dict[str, Decimal]]] = defaultdict(lambda: defaultdict(dict))
    totals: dict[date, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for row in rows:
        day, model = date.fromisoformat(row["event_date_utc"]), row["attribution_model_key"]
        revenue = Decimal(row["revenue_attributed_deduplicated_incl_halo"])
        credit[day][row["ad_product_type"]][model] = revenue
        totals[day][model] += revenue
    return sorted(
        day
        for day, channels in credit.items()
        if any(len(set(models.values())) > 1 for models in channels.values())
        and max(totals[day].values()) >= ceiling.get(day, Decimal(0))
    )


def _two_channel_day(day: date, first_touch_dsp: tuple[str, str]) -> Rows:
    """Build one day where linear credits DSP and SP 10 / 300.00 each, first touch DSP as given."""
    first_sp = (
        str(20 - Decimal(first_touch_dsp[0])),
        str(Decimal("600.00") - Decimal(first_touch_dsp[1])),
    )
    cells = {
        ("linear_v1", DSP): ("10", "300.00"),
        ("linear_v1", "sponsored_products"): ("10", "300.00"),
        ("first_touch_v1", DSP): first_touch_dsp,
        ("first_touch_v1", "sponsored_products"): first_sp,
    }
    return [
        {
            "event_date_utc": day.isoformat(),
            "ad_product_type": channel,
            "attribution_model_key": model,
            "matched_purchase_credit": credit,
            "revenue_attributed_deduplicated_incl_halo": revenue,
        }
        for (model, channel), (credit, revenue) in cells.items()
    ]


def test_no_model_credits_a_channel_more_than_its_own_attributed_measure() -> None:
    assert credit_above_own_attributed(_deduplicated(), _attributed()) == []
    # Positive control: first touch giving DSP 12 / 360.00 against its own 10 / 300.00 is caught.
    day = date(2026, 9, 30)
    attributed = [
        _attributed_day(day, DSP, (10, "300.00")),
        _attributed_day(day, "sponsored_products", (10, "300.00")),
    ]
    assert credit_above_own_attributed(_two_channel_day(day, ("12", "360.00")), attributed) == [
        (day, "first_touch_v1", DSP)
    ]
    assert credit_above_own_attributed(_two_channel_day(day, ("10", "300.00")), attributed) == []


def test_models_split_channels_differently_only_below_the_attributed_sum() -> None:
    assert days_split_by_model_without_overlap(_deduplicated(), _attributed()) == []
    # Positive control: a 600.00 day equal to the attributed sum on which first touch moves
    # 60.00 to DSP is caught; the same split under a 700.00 attributed sum is not.
    day = date(2026, 9, 30)
    rows = _two_channel_day(day, ("8", "240.00"))
    equal = [
        _attributed_day(day, DSP, (10, "300.00")),
        _attributed_day(day, "sponsored_products", (10, "300.00")),
    ]
    above = [*equal[:1], _attributed_day(day, "sponsored_products", (14, "400.00"))]
    assert days_split_by_model_without_overlap(rows, equal) == [day]
    assert days_split_by_model_without_overlap(rows, above) == []
