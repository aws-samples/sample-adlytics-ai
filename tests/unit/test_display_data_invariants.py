# SPDX-License-Identifier: MIT-0
"""Owner decision #8 (2026-10-05): fix the data before display, never flag it on the page.

The 2026-10-04 UX review found three data-shape defects on the earlier dev generation:
returns above shipped units (F16), an implausibly small prior retail week (F11), and the same
CTR on every campaign (F24). The shaped seed (ADR-086) removes them; these invariants pin it
over the committed fixtures, and each check is shown to catch the defect it guards.
"""

from __future__ import annotations

import csv
import json
import statistics
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-086")]
GENERATED = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "generated"
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
