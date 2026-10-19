# SPDX-License-Identifier: MIT-0
"""Lay the exposure-groups fixture (pack 5) out as one delivery of its 3 x 2 grid per period.

Pack 5 (`amc-exposure-groups`, ADR-101) is executed once per complete ISO week and calendar month,
and each delivery is three exposure source sets by two identity spaces. No value is random: a
period's DSP and sponsored impressions and raw costs are the `amc-dsp-impressions` and
`amc-reach-frequency` corpora's over its days, split by the declared shares
(`ExposureGroupsShape`), and every count follows from them by the declared rates. So shoppers
never exceed impressions, and each outcome count never exceeds the shoppers it is drawn from.

The shaped cells mirror the live multi-source suppression shape (Sandbox Q3s, 2026-10-09,
`validation/RESULTS.md`): a withheld row keeps its metric-derived `exposure_source_set`, its
distinct counts and exposure-side sums, nulls its `identity_space`, marketplace, purchase-side
sums and every currency, and names one source path in a `(path i of n)` reason. Which path
number AMC gives each source is [unverified]; the fixture numbers them in `source_tables` order.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from typing import TYPE_CHECKING, Any

from adapters.jobs.fixture_generation.periodic import complete_periods, period_window
from adapters.jobs.fixture_generation.shapes import Fixture, FixtureError, declaration_for
from domain.semantics.calendar_periods import CalendarPeriod
from domain.semantics.time import TimeGrain

if TYPE_CHECKING:  # pragma: no cover — typing only
    from adapters.jobs.fixture_generation.catalog import SyntheticEntityCatalog
    from adapters.jobs.fixture_generation.conformance import SuppressionOutput
    from adapters.jobs.fixture_generation.shape import ExposureCellShape, ExposureGroupsShape
    from adapters.jobs.fixture_generation.shapes import FixtureBuildOptions
    from domain.data_contracts.contract import DataContract
    from domain.producer.models import GovernedQueryPack

__all__ = [
    "EXPOSURE_CONTRACT",
    "ExposureCorpus",
    "PathReason",
    "build_exposure_groups",
]

EXPOSURE_CONTRACT = "amc-exposure-groups"
DSP_CORPUS = "amc-dsp-impressions"
SPONSORED_CORPUS = "amc-reach-frequency"
SOURCE_SETS = ("dsp", "sponsored", "dsp+sponsored")
IDENTITY_SPACES = ("account", "device_or_browser")
_SPONSORED_CLICK_DIVISOR = Decimal(100_000_000)
"""`sponsored_ads_traffic.spend` is microcents (÷1e8), so a click's spend is written raw."""


@dataclass(frozen=True, slots=True)
class PathReason:
    """One multi-source suppressed row's reason: its users, the short path, and the bar."""

    users: int
    table: str
    path: int
    paths: int
    bar: int

    @property
    def text(self) -> str:
        """Render the reason in the live `(path i of n)` form."""
        return (
            f"{self.users} '{self.table}' (path {self.path} of {self.paths}) users < "
            f"{self.bar} required"
        )


@dataclass(frozen=True, slots=True)
class ExposureCorpus:
    """The DSP and sponsored delivery corpora a period's touches are drawn from."""

    dsp: tuple[Mapping[str, Any], ...]
    sponsored: tuple[Mapping[str, Any], ...]

    def totals(self, period: CalendarPeriod) -> tuple[int, int, int, int, str]:
        """Return DSP impressions and raw cost, sponsored impressions and raw spend, currency."""

        def within(row: Mapping[str, Any], column: str) -> bool:
            return period.start <= date.fromisoformat(str(row[column])) <= period.end

        dsp = [row for row in self.dsp if within(row, "impression_date_utc")]
        sponsored = [row for row in self.sponsored if within(row, "event_date_utc")]
        currencies = {str(row["currency_iso_code"]) for row in (*dsp, *sponsored)}
        if len(currencies) != 1:
            raise FixtureError(f"{period.start}: the touch corpora carry {currencies} currencies.")
        return (
            sum(int(row["impressions"]) for row in dsp),
            sum(int(row["total_cost"]) for row in dsp),
            sum(int(row["impressions"]) for row in sponsored),
            sum(int(row["spend"]) for row in sponsored),
            currencies.pop(),
        )


def _floor(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_FLOOR))


@dataclass(frozen=True, slots=True)
class _Cell:
    source_set: str
    identity_space: str


def _matches(shaped: ExposureCellShape, period: CalendarPeriod, cell: _Cell) -> bool:
    return (
        shaped.period_start == period.start
        and shaped.exposure_source_set == cell.source_set
        and shaped.identity_space == cell.identity_space
    )


def _fractions(shape: ExposureGroupsShape, source_set: str) -> tuple[Decimal, Decimal]:
    """Return the share of the DSP side and of the sponsored side a source set holds."""
    dsp_rest, sponsored_rest = 1 - shape.dsp_only_share, 1 - shape.sponsored_only_share
    return {
        "dsp": (shape.dsp_only_share, Decimal(0)),
        "sponsored": (Decimal(0), shape.sponsored_only_share),
        "dsp+sponsored": (dsp_rest, sponsored_rest),
    }[source_set]


@dataclass(frozen=True, slots=True)
class _Layout:
    contract: DataContract
    pack: GovernedQueryPack
    shape: SuppressionOutput
    header: tuple[str, ...]
    groups: ExposureGroupsShape
    marketplace: str


def _kept(
    layout: _Layout, slot: tuple[CalendarPeriod, _Cell], totals: tuple[int, int, int, int, str]
) -> dict[str, Any]:
    """Build one returned cell from the period's touch totals and the declared rates."""
    period, cell = slot
    shape = layout.groups
    dsp_imps, dsp_cost, sponsored_imps, sponsored_spend, currency = totals
    dsp_part, sponsored_part = _fractions(shape, cell.source_set)
    space = shape.account_share if cell.identity_space == "account" else 1 - shape.account_share
    impressions = _floor((dsp_part * dsp_imps + sponsored_part * sponsored_imps) * space)
    dsp_spend = _floor(dsp_part * dsp_cost * space)
    sponsored = _floor(sponsored_part * sponsored_spend * space)
    shoppers = impressions // shape.impressions_per_shopper
    if _matches(shape.click_cell, period, cell):
        # Sponsored Products shoppers with 3 impressions and one billed click each inside W: the
        # click row carries the spend and starts no window, so window spend is the clicks' spend.
        shoppers = shape.click_shoppers
        impressions = 3 * shoppers
        sponsored = _floor(shape.click_spend * shoppers * _SPONSORED_CLICK_DIVISOR)
    factor = Decimal(1) if cell.identity_space == "account" else shape.device_rate_factor
    purchasers = _floor(shoppers * shape.purchaser_rate * factor)
    purchases = _floor(purchasers * shape.purchases_per_purchaser)
    return {
        "exposure_source_set": cell.source_set,
        "identity_space": cell.identity_space,
        "row_kind": "combined",
        "shoppers_reached": shoppers,
        "impressions": impressions,
        "dsp_window_spend": dsp_spend,
        "sponsored_window_spend": sponsored,
        "dsp_currency": currency if dsp_part else None,
        "dsp_currency_min": currency if dsp_part else None,
        "sponsored_currency": currency if sponsored_part else None,
        "sponsored_currency_min": currency if sponsored_part else None,
        "search_shoppers": _floor(shoppers * shape.search_rate * factor),
        "detail_page_view_shoppers": _floor(shoppers * shape.detail_page_view_rate * factor),
        "purchasers": purchasers,
        "ntb_purchasers": _floor(purchasers * shape.ntb_share),
        "purchases": purchases,
        "revenue_incl_halo": f"{shape.order_value * purchases:.2f}",
        "currency_code": currency if purchases else None,
        "purchase_currency_min": currency if purchases else None,
        "marketplace_name": layout.marketplace,
        "distinct_user_count": layout.pack.expected_bar,
        "suppression_reason": None,
        "rows_suppressed": False,
    }


def _few(
    kept: dict[str, Any], totals: tuple[int, int, int, int, str], counts: tuple[int, int]
) -> dict[str, Any]:
    """Shrink a cell to a few shoppers, each with the declared impressions, at the period's rates.

    Every shopper except one bought, searched and viewed, so the per-path case holds two or more
    purchasers as the live withheld row did, and a one-shopper cell none.
    """
    shoppers, per_shopper = counts
    dsp_imps, dsp_cost, sponsored_imps, sponsored_spend, _currency = totals
    impressions = shoppers * per_shopper
    outcome = max(shoppers - 1, 0)
    return kept | {
        "shoppers_reached": shoppers,
        "impressions": impressions,
        "dsp_window_spend": impressions * dsp_cost // dsp_imps if kept["dsp_window_spend"] else 0,
        "sponsored_window_spend": (
            impressions * sponsored_spend // sponsored_imps if kept["sponsored_window_spend"] else 0
        ),
        "search_shoppers": outcome,
        "detail_page_view_shoppers": outcome,
        "purchasers": outcome,
        "ntb_purchasers": min(outcome, 1),
    }


def _withheld(layout: _Layout, kept: dict[str, Any], users: tuple[int, int]) -> dict[str, Any]:
    """Withhold a cell the live multi-source way, naming the source path that fell under the bar.

    `users` is the row's shoppers and the path whose count fell short (1-based in
    `source_tables`). Distinct counts and exposure-side sums survive; the identity space,
    marketplace, purchase-side sums and every currency are NULL.
    """
    shoppers, path = users
    tables = layout.contract.source_tables
    return {
        **kept,
        "identity_space": None,
        "marketplace_name": None,
        "shoppers_reached": shoppers,
        **dict.fromkeys(
            (
                "dsp_currency",
                "dsp_currency_min",
                "sponsored_currency",
                "sponsored_currency_min",
                "purchases",
                "revenue_incl_halo",
                "currency_code",
                "purchase_currency_min",
            )
        ),
        "distinct_user_count": 1,
        "suppression_reason": PathReason(
            1, tables[path - 1], path, len(tables), layout.pack.expected_bar
        ).text,
        "rows_suppressed": True,
    }


def _rows(
    layout: _Layout, corpus: ExposureCorpus, catalog: SyntheticEntityCatalog
) -> Iterator[dict[str, Any]]:
    policy = layout.pack.period_policy
    if policy is None:  # pragma: no cover — pack 5 is periodic
        raise FixtureError(f"{layout.pack.pack_id}: not a periodic pack.")
    span = catalog.shape.fixture_span
    shape = layout.groups
    for period in complete_periods(
        tuple(grain.value for grain in policy.grains), span.first_day, span.last_day
    ):
        start, end = period_window(period, layout.shape.timestamp_format)
        window = {"time_window_start": start, "time_window_end": end}
        totals = corpus.totals(period)
        for source_set in SOURCE_SETS:
            for identity_space in IDENTITY_SPACES:
                cell = _Cell(source_set, identity_space)
                kept = _kept(layout, (period, cell), totals)
                if period.grain is TimeGrain.WEEK and _matches(shape.not_returned, period, cell):
                    # One shopper in this cell, below the bar of 2 on the DSP path.
                    few = _few(kept, totals, (1, shape.impressions_per_shopper))
                    yield _row(layout, window | _withheld(layout, few, (1, 1)))
                    continue
                if period.grain is TimeGrain.MONTH and _matches(shape.withheld_path, period, cell):
                    # Two or more shoppers, but fewer than two reached it through the sponsored
                    # path: withheld per path, and discarded like any withheld row (plan D19).
                    few = _few(
                        kept, totals, (shape.withheld_shoppers, shape.impressions_per_shopper)
                    )
                    yield _row(
                        layout, window | _withheld(layout, few, (shape.withheld_shoppers, 2))
                    )
                    continue
                if (
                    period.grain is TimeGrain.WEEK
                    and period.start == shape.currency_disagreement_week
                    and source_set == "dsp+sponsored"
                    and identity_space == "account"
                ):
                    # Two currencies reached this row's sponsored touches: refused at load.
                    kept = kept | {"sponsored_currency_min": "CAD"}
                yield _row(layout, window | kept)


def _row(layout: _Layout, values: Mapping[str, Any]) -> dict[str, Any]:
    return {name: values[name] for name in layout.header}


def build_exposure_groups(
    pack: GovernedQueryPack,
    sources: tuple[DataContract, SuppressionOutput, FixtureBuildOptions],
    corpus: ExposureCorpus,
) -> Fixture:
    """Build pack 5's fixture: the 3 x 2 grid for every complete week and month, oldest first."""
    contract, shape, options = sources
    catalog = options.entity_catalog
    if catalog is None:
        raise FixtureError(f"{contract.contract_id}: a periodic fixture needs the entity catalog.")
    trailer = shape.trailing_order
    header = (*(name for name in pack.expected_columns if name not in trailer), *trailer)
    layout = _Layout(
        contract,
        pack,
        shape,
        header,
        catalog.shape.exposure_groups,
        catalog.marketplace_aliases["amc.marketplace_name"],
    )
    return Fixture(
        contract_id=contract.contract_id,
        source_format=contract.source_format,
        manifest=declaration_for(contract, options),
        rows=tuple(_rows(layout, corpus, catalog)),
    )
