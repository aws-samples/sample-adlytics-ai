# SPDX-License-Identifier: MIT-0
"""Vendor source-document consistency helpers for generated fictional fixtures."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from decimal import Decimal
from typing import cast

from adapters.jobs.fixture_generation.shape import CatalogShape, ReconciliationMismatch


class VendorFixtureError(ValueError):
    """A generated Vendor document cannot satisfy its source control relationship."""


def reported_units_adjustments(shape: CatalogShape) -> dict[str, int]:
    """Map each declared reconciliation-mismatch day to its reported-total units delta."""
    return {
        event.on.isoformat(): event.units_delta
        for event in shape.events
        if isinstance(event, ReconciliationMismatch)
    }


def reconcile_vendor_aggregates(
    document: dict[str, object], units_adjustments: Mapping[str, int] | None = None
) -> dict[str, object]:
    """Derive every reported aggregate exactly from same-day detail rows.

    `units_adjustments` is the one declared exception (design §3.1 `reconciliation_mismatch`):
    on its day the reported shipped units differ from the detail sum by the declared delta, so
    the control-total check meets a real mismatch.

    The aggregate is source evidence, not an independently generated scenario. Generating it
    from the same declared scenario at another grain would almost certainly disagree; this
    helper makes the source document's control-total relationship explicit and exact.
    """
    if "salesByAsin" in document:
        _reconcile_sales(document, units_adjustments or {})
    if "trafficByAsin" in document:
        _reconcile_traffic(document)
    return document


def _rows_by_day(rows: list[dict[str, object]], label: str) -> dict[str, list[dict[str, object]]]:
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        day = row.get("startDate")
        if not isinstance(day, str) or not day:
            raise VendorFixtureError(f"{label} row has no startDate.")
        grouped[day].append(row)
    return dict(grouped)


def _total_by_day(rows: list[dict[str, object]], label: str) -> dict[str, dict[str, object]]:
    grouped = _rows_by_day(rows, label)
    if any(len(values) != 1 for values in grouped.values()):
        raise VendorFixtureError(f"{label} requires exactly one aggregate per day.")
    return {day: values[0] for day, values in grouped.items()}


def _reconcile_sales(document: dict[str, object], units_adjustments: Mapping[str, int]) -> None:
    detail = cast("list[dict[str, object]]", document["salesByAsin"])
    totals = cast("list[dict[str, object]]", document["salesAggregate"])
    detail_by_day = _rows_by_day(detail, "Vendor sales detail")
    total_by_day = _total_by_day(totals, "Vendor sales aggregate")
    if set(detail_by_day) != set(total_by_day):
        raise VendorFixtureError("Vendor sales detail and aggregate days differ.")
    for day, rows in detail_by_day.items():
        total = total_by_day[day]
        for field in ("shippedUnits", "orderedUnits", "customerReturns"):
            total[field] = sum(int(cast("int", row[field])) for row in rows)
        total["shippedUnits"] = int(cast("int", total["shippedUnits"])) + units_adjustments.get(
            day, 0
        )
        for field in ("shippedRevenue", "shippedCogs", "orderedRevenue"):
            amounts = [cast("dict[str, object]", row[field]) for row in rows]
            total[field] = {
                "amount": float(
                    sum((Decimal(str(amount["amount"])) for amount in amounts), Decimal(0))
                ),
                "currencyCode": "USD",
            }


def _reconcile_traffic(document: dict[str, object]) -> None:
    detail = cast("list[dict[str, object]]", document["trafficByAsin"])
    totals = cast("list[dict[str, object]]", document["trafficAggregate"])
    detail_by_day = _rows_by_day(detail, "Vendor traffic detail")
    total_by_day = _total_by_day(totals, "Vendor traffic aggregate")
    if set(detail_by_day) != set(total_by_day):
        raise VendorFixtureError("Vendor traffic detail and aggregate days differ.")
    for day, rows in detail_by_day.items():
        total_by_day[day]["glanceViews"] = sum(int(cast("int", row["glanceViews"])) for row in rows)
