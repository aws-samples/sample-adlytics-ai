# SPDX-License-Identifier: MIT-0
"""Lay the cohort fixtures (packs 14 and 15) out as one delivery of every cell per vintage.

Packs 14 and 15 (`amc-acquisition-cohorts` and `-arp`, design §6.6 B3) are executed once per
cohort vintage: the 13 calendar months ending with its as-of month. Each delivery is every cell of
the vintage's triangle (`domain.shopper_aggregates.cohorts`) by three acquisition source sets. No
value is random: every count follows from `AcquisitionCohortsShape`, so cohort sizes repeat on
every follow-up row and cumulative repeaters sum the first-time ones by construction.

Three vintages are delivered, oldest first. The refused vintage repeats one cohort size
differently on one follow-up row, so the load refuses it (REPEATED_TOTAL_MISMATCH) and it is
never landed. The degraded vintage is monthly: its absent cohort and group have no row at any k,
one cell of three shoppers comes back withheld per path in AMC's multi-source shape (keys, counts,
repeat revenue and currency NULL; plan D19), and the oldest cohort's dsp follow-ups 7 to 12 don't
come back, so the dsp slice grades degraded. The quarterly vintage is the calendar-quarter variant
that grade switched on (plan D10). Pack 15 delivers the degraded vintage monthly and the quarterly
one quarterly; on the degraded vintage its own count of one cohort is one under pack 14's, and the
same cell comes back withheld on its second path, so the per-path bar holds on both paths.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from typing import TYPE_CHECKING, Any

from adapters.jobs.fixture_generation.exposure import PathReason
from adapters.jobs.fixture_generation.periodic import period_window
from adapters.jobs.fixture_generation.shapes import Fixture, FixtureError, declaration_for
from domain.data_contracts.contract import CohortGrain, CohortTriangle
from domain.semantics.calendar_periods import vintage_ending
from domain.shopper_aggregates.cohorts import triangle_cohorts

if TYPE_CHECKING:  # pragma: no cover — typing only
    from adapters.jobs.fixture_generation.conformance import SuppressionOutput
    from adapters.jobs.fixture_generation.shape import AcquisitionCohortsShape
    from adapters.jobs.fixture_generation.shapes import FixtureBuildOptions
    from domain.data_contracts.contract import DataContract
    from domain.producer.models import GovernedQueryPack

__all__ = [
    "COHORT_ARP",
    "COHORT_CONTRACTS",
    "COHORT_STANDARD",
    "build_acquisition_cohorts",
    "cohort_vintages",
]

COHORT_STANDARD = "amc-acquisition-cohorts"
COHORT_ARP = "amc-acquisition-cohorts-arp"
COHORT_CONTRACTS = (COHORT_STANDARD, COHORT_ARP)
SOURCE_SETS = {
    "dsp_only": "dsp",
    "sponsored_only": "sponsored",
    "dsp_and_sponsored": "dsp+sponsored",
}
"""The source set each acquisition channel group resolves from (media taxonomy, ADR-101)."""
_TRIANGLES = {
    CohortGrain.MONTH: CohortTriangle(
        "cohort_month", "months_since_acquisition", CohortGrain.MONTH, 12
    ),
    CohortGrain.QUARTER: CohortTriangle(
        "cohort_month", "months_since_acquisition", CohortGrain.QUARTER, 3
    ),
}
_QUARTER_MONTHS = 3
_DEGRADED_FROM = 7
"""The oldest cohort's dsp follow-ups from month 7 on don't come back on the degraded vintage."""
_REFUSED_K = 5
_CONVERSIONS_PATH = 2
"""`conversions` is pack 14's second source table, the path the withheld cell lacks."""


def _floor(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_FLOOR))


def cohort_vintages(
    shape: AcquisitionCohortsShape, contract_id: str
) -> tuple[tuple[date, CohortGrain], ...]:
    """Each delivered vintage's as-of month and cohort grain, oldest first."""
    delivered = (
        (shape.degraded_vintage, CohortGrain.MONTH),
        (shape.quarterly_vintage, CohortGrain.QUARTER),
    )
    if contract_id == COHORT_STANDARD:
        return ((shape.refused_vintage, CohortGrain.MONTH), *delivered)
    return delivered


@dataclass(frozen=True, slots=True)
class _Layout:
    contract: DataContract
    pack: GovernedQueryPack
    shape: SuppressionOutput
    header: tuple[str, ...]
    cohorts: AcquisitionCohortsShape
    marketplace: str
    currency: str


def _sizes(layout: _Layout, group: str, months: int) -> tuple[int, int]:
    """Return a cohort's buyers and account IDs for one group, `months` cohort months together."""
    shares = dict(zip(SOURCE_SETS, layout.cohorts.group_shares, strict=True))
    size = _floor(Decimal(layout.cohorts.cohort_buyers * months) * shares[group])
    return size, _floor(Decimal(size) * layout.cohorts.account_share)


def _rate(layout: _Layout, grain: CohortGrain, k: int) -> Decimal:
    rate = layout.cohorts.repeat_rates[k - 1]
    return rate * layout.cohorts.quarter_lift if grain is CohortGrain.QUARTER else rate


def _standard_cell(
    layout: _Layout, slot: tuple[str, CohortGrain, int, str], sizes: tuple[int, int]
) -> dict[str, Any]:
    cohort, grain, k, group = slot
    size, accounts = sizes
    shape = layout.cohorts
    repeat = _floor(Decimal(size) * _rate(layout, grain, k))
    first = _floor(Decimal(repeat) * shape.first_repeat_share)
    cumulative = sum(
        _floor(Decimal(_floor(Decimal(size) * _rate(layout, grain, j))) * shape.first_repeat_share)
        for j in range(1, k + 1)
    )
    purchases = _floor(Decimal(repeat) * shape.purchases_per_buyer)
    currency = layout.currency if purchases else None
    return {
        "cohort_month": cohort,
        "months_since_acquisition": k,
        "acquisition_source_set": SOURCE_SETS[group],
        "cohort_buyers_new_to_brand_12mo": size,
        "cohort_buyers_account_ids": accounts,
        "repeat_buyers": repeat,
        "repeat_buyers_account_ids": _floor(Decimal(repeat) * shape.account_share),
        "first_repeat_buyers": first,
        "cumulative_repeat_buyers": cumulative,
        "cumulative_repeat_buyers_account_ids": _floor(Decimal(cumulative) * shape.account_share),
        "repeat_purchases": purchases,
        "repeat_revenue": f"{shape.order_value * purchases:.2f}",
        "purchase_currency": currency,
        "purchase_currency_min": currency,
    }


def _arp_cell(
    layout: _Layout, slot: tuple[str, CohortGrain, int, str], accounts: int
) -> dict[str, Any]:
    cohort, grain, k, group = slot
    shape = layout.cohorts

    def repeaters(j: int) -> int:
        return min(accounts, _floor(Decimal(accounts) * _rate(layout, grain, j) * shape.arp_lift))

    repeat = repeaters(k)
    cumulative = min(
        accounts,
        sum(_floor(Decimal(repeaters(j)) * shape.first_repeat_share) for j in range(1, k + 1)),
    )
    purchases = _floor(Decimal(repeat) * shape.purchases_per_buyer)
    currency = layout.currency if purchases else None
    return {
        "cohort_month": cohort,
        "months_since_acquisition": k,
        "acquisition_source_set": SOURCE_SETS[group],
        "cohort_buyers_account_ids_check": accounts,
        "repeat_buyers": repeat,
        "first_repeat_buyers": _floor(Decimal(repeat) * shape.first_repeat_share),
        "cumulative_repeat_buyers": cumulative,
        "repeat_purchases": purchases,
        "repeat_revenue": f"{shape.order_value * purchases:.2f}",
        "currency_code": currency,
        "currency_code_min": currency,
    }


def _withheld(layout: _Layout, kept: dict[str, Any]) -> dict[str, Any]:
    """Withhold a cell the live multi-source way, naming the short conversions path (plan D19)."""
    tables = layout.contract.source_tables
    nulled = {
        name: None
        for name in kept
        if name not in {"repeat_purchases", "cohort_grain", "time_window_start", "time_window_end"}
    }
    return {
        **kept,
        **nulled,
        "repeat_purchases": layout.cohorts.withheld_shoppers,
        "distinct_user_count": 1,
        "suppression_reason": PathReason(
            1,
            tables[_CONVERSIONS_PATH - 1],
            _CONVERSIONS_PATH,
            len(tables),
            layout.pack.expected_bar,
        ).text,
        "rows_suppressed": True,
    }


def _vintage_rows(layout: _Layout, vintage: tuple[date, CohortGrain]) -> Iterator[dict[str, Any]]:
    as_of, grain = vintage
    period = vintage_ending(as_of)
    shape = layout.cohorts
    standard = layout.contract.contract_id == COHORT_STANDARD
    start, end = period_window(period, layout.shape.timestamp_format)
    trailer = {
        "cohort_grain": grain.value,
        "marketplace_name": layout.marketplace,
        "time_window_start": start,
        "time_window_end": end,
        "distinct_user_count": layout.pack.expected_bar,
        "suppression_reason": None,
        "rows_suppressed": False,
    }
    months = 1 if grain is CohortGrain.MONTH else _QUARTER_MONTHS
    degraded = as_of == shape.degraded_vintage
    cohorts = triangle_cohorts(_TRIANGLES[grain], (period.start, period.end))
    oldest = cohorts[0].start
    for group in SOURCE_SETS:
        for cohort in cohorts:
            if degraded and (cohort.start, group) == (shape.absent_cohort, shape.absent_group):
                # An absent cohort: no shopper was won this way that month, or too few.
                continue
            size, accounts = _sizes(layout, group, months)
            for k in cohort.offsets:
                if (
                    degraded
                    and (cohort.start, group) == (oldest, "dsp_only")
                    and k >= _DEGRADED_FROM
                ):
                    continue
                slot = (cohort.start.isoformat(), grain, k, group)
                if standard:
                    cell = _standard_cell(layout, slot, (size, accounts))
                    if as_of == shape.refused_vintage and (cohort.start, group, k) == (
                        oldest,
                        "sponsored_only",
                        _REFUSED_K,
                    ):
                        # One copy of the cohort's size differs: the delivery is refused.
                        cell["cohort_buyers_new_to_brand_12mo"] = size + 1
                else:
                    check = accounts
                    if degraded and (cohort.start, group) == (
                        shape.arp_check_gap_cohort,
                        shape.arp_check_gap_group,
                    ):
                        # Pack 15's own count is one under pack 14's: the rate is withheld.
                        check = accounts - 1
                    cell = _arp_cell(layout, slot, check)
                kept = cell | trailer
                if degraded and (cohort.start, k, group) == (
                    shape.withheld_cohort,
                    shape.withheld_k,
                    shape.withheld_group,
                ):
                    yield _row(layout, _withheld(layout, kept))
                    continue
                yield _row(layout, kept)


def _row(layout: _Layout, values: Mapping[str, Any]) -> dict[str, Any]:
    return {name: values[name] for name in layout.header}


def build_acquisition_cohorts(
    pack: GovernedQueryPack,
    sources: tuple[DataContract, SuppressionOutput, FixtureBuildOptions],
    currency: str,
) -> Fixture:
    """Build pack 14's or pack 15's fixture: every cell of each delivered vintage."""
    contract, shape, options = sources
    catalog = options.entity_catalog
    if catalog is None:
        raise FixtureError(f"{contract.contract_id}: a cohort fixture needs the entity catalog.")
    trailer = shape.trailing_order
    header = (*(name for name in pack.expected_columns if name not in trailer), *trailer)
    layout = _Layout(
        contract,
        pack,
        shape,
        header,
        catalog.shape.acquisition_cohorts,
        catalog.marketplace_aliases["amc.marketplace_name"],
        currency,
    )
    # A vintage starting before the span's first day is outside a shortened horizon.
    first_day = catalog.shape.fixture_span.first_day
    rows = tuple(
        row
        for vintage in cohort_vintages(layout.cohorts, contract.contract_id)
        if vintage_ending(vintage[0]).start >= first_day
        for row in _vintage_rows(layout, vintage)
    )
    return Fixture(
        contract_id=contract.contract_id,
        source_format=contract.source_format,
        manifest=declaration_for(contract, options),
        rows=rows,
    )
