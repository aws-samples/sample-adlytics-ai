# SPDX-License-Identifier: MIT-0
"""Lay a periodic shopper-aggregate pack's fixture out as one delivery per complete period.

A periodic pack (ADR-100) is executed once per complete ISO week and once per calendar month, and
each execution's window is exactly its period. So its fixture is the concatenation of those
deliveries: every row carries its own period's AMC window in the live timestamp format, the
header is the pack's `outputColumns` with the three option columns last in the live order, and a
kept row's `distinct_user_count` is the pack's bar. `land_fixtures` splits the file back into one
delivery per window.

No value is random. Pack 1's distinct buyers are drawn from the attributed-conversion corpus the
same file set already carries: `buyers_per_purchase` times the period's credited purchases,
rounded down, so a buyer count is bounded by the purchases it could have made. The one declared
`not_returned_week` returns only a suppressed one-shopper row with its source keys NULL, as AMC
delivered one live (`suppression_output` in `contracts/shared/amc-columns.yaml`).

Pack 2 splits each period by ad product (`BuyerBound.split_column`): one row per ad product with
a credited purchase in the period, each drawn from that ad product's own purchases, so it is
bounded by them and by the period's platform buyers. Its declared missing ad product returns a
suppressed row in its week, and pack 1's not-returned week returns one suppressed row here too.

Pack 3 splits each calendar month by campaign the same way, carrying each campaign's ad product
from the corpus (`BuyerBound.carried_columns`). Its two shaped months drop campaigns: the
withdrawn month the first quarter of them by source key, the other one declared campaign. The
campaigns a period drops come back as one merged suppressed row, keys NULL. That AMC merges
rows whose keys it nulls is [unverified] here, and design §4.4 records live evidence of the
opposite under a selected distinct count (duplicate fully-nulled rows kept). The load discards
either shape before the fact write, so one merged row still exercises the served outcome.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import TYPE_CHECKING, Any

from adapters.jobs.fixture_generation.shapes import Fixture, FixtureError, declaration_for
from domain.semantics.calendar_periods import CalendarPeriod, period_containing
from domain.semantics.time import TimeGrain

if TYPE_CHECKING:  # pragma: no cover — typing only
    from adapters.jobs.fixture_generation.catalog import SyntheticEntityCatalog
    from adapters.jobs.fixture_generation.conformance import SuppressionOutput
    from adapters.jobs.fixture_generation.shapes import FixtureBuildOptions
    from domain.data_contracts.contract import DataContract
    from domain.producer.models import GovernedQueryPack

__all__ = [
    "BUYER_BOUNDS",
    "BuyerBound",
    "PeriodicCorpus",
    "build_periodic",
    "complete_periods",
    "period_window",
]

_PERIOD_GRAINS = {"week": TimeGrain.WEEK, "month": TimeGrain.MONTH, "quarter": TimeGrain.QUARTER}


@dataclass(frozen=True, slots=True)
class BuyerBound:
    """Which corpus purchases bound one periodic pack's distinct-buyer columns (design §5.5).

    `pairs` maps each buyer column to the attributed-corpus purchases column it can't exceed:
    every buyer made at least one purchase, and a credited purchase total counts each purchase
    at least once.
    """

    corpus_contract: str
    date_column: str
    marketplace_column: str
    marketplace_target: str
    pairs: tuple[tuple[str, str], ...]
    split_column: str | None = None
    """The corpus and fixture column a finer grain set splits by (pack 2's `ad_product_type`):
    each row is bounded by its own value's purchases only, never the whole period's."""
    platform_contract: str | None = None
    """The coarser grain set's pack whose same-period row bounds each split row (pack 1)."""
    carried_columns: tuple[str, ...] = ()
    """Corpus columns a split row carries from its split value's corpus rows (pack 3's
    `ad_product_type`: a campaign runs on one ad product)."""


_PURCHASE_PAIRS: tuple[tuple[str, str], ...] = (
    ("buyers_new_to_brand_12mo", "new_to_brand_purchases"),
    ("buyers_attributed", "purchases"),
)

BUYER_BOUNDS: Mapping[str, BuyerBound] = {
    "amc-new-to-brand-buyers-platform": BuyerBound(
        corpus_contract="amc-attributed-conversion",
        date_column="conversion_event_date_utc",
        marketplace_column="conversion_event_marketplace_name",
        marketplace_target="conversion_event_marketplace_name",
        pairs=_PURCHASE_PAIRS,
    ),
    "amc-new-to-brand-buyers-channel": BuyerBound(
        corpus_contract="amc-attributed-conversion",
        date_column="conversion_event_date_utc",
        marketplace_column="conversion_event_marketplace_name",
        marketplace_target="conversion_event_marketplace_name",
        pairs=_PURCHASE_PAIRS,
        split_column="ad_product_type",
        platform_contract="amc-new-to-brand-buyers-platform",
    ),
    "amc-new-to-brand-buyers-campaign": BuyerBound(
        corpus_contract="amc-attributed-conversion",
        date_column="conversion_event_date_utc",
        marketplace_column="conversion_event_marketplace_name",
        marketplace_target="conversion_event_marketplace_name",
        pairs=_PURCHASE_PAIRS,
        split_column="campaign_id_string",
        platform_contract="amc-new-to-brand-buyers-platform",
        carried_columns=("ad_product_type",),
    ),
}
"""The periodic packs and the corpus that bounds them; the conformance check reads the same."""


@dataclass(frozen=True, slots=True)
class PeriodicCorpus:
    """The attributed-conversion rows a periodic pack's buyers are drawn from."""

    rows: tuple[Mapping[str, Any], ...]

    def total(
        self,
        bound: BuyerBound,
        column: str,
        request: tuple[CalendarPeriod, str] | tuple[CalendarPeriod, str, str],
    ) -> int:
        """Sum one purchases column over the period's days for one marketplace (and split) value."""
        period, marketplace, *split = request
        return sum(
            int(row[column])
            for row in self.rows
            if row[bound.marketplace_column] == marketplace
            and period.start <= date.fromisoformat(str(row[bound.date_column])) <= period.end
            and (not split or bound.split_column is None or row[bound.split_column] == split[0])
        )

    def split_values(
        self, bound: BuyerBound, request: tuple[CalendarPeriod, str]
    ) -> tuple[str, ...]:
        """Every split value with a credited purchase in the period, for one marketplace, sorted."""
        period, marketplace = request
        column = bound.split_column
        if column is None:
            return ()
        return tuple(
            sorted(
                {
                    str(row[column])
                    for row in self.rows
                    if row[bound.marketplace_column] == marketplace
                    and int(row[bound.pairs[-1][1]]) > 0
                    and period.start
                    <= date.fromisoformat(str(row[bound.date_column]))
                    <= period.end
                }
            )
        )

    def carried(self, bound: BuyerBound, value: str) -> dict[str, Any]:
        """Return the carried columns' one value for a split value, or refuse two."""
        found: dict[str, Any] = {}
        for column in bound.carried_columns:
            values = {
                str(row[column])
                for row in self.rows
                if bound.split_column is not None and row[bound.split_column] == value
            }
            if len(values) != 1:
                raise FixtureError(
                    f"{bound.split_column} {value} carries {len(values)} {column} values in the "
                    "corpus; a split row carries exactly one."
                )
            found[column] = values.pop()
        return found


def complete_periods(
    grains: tuple[str, ...], first_day: date, last_day: date
) -> tuple[CalendarPeriod, ...]:
    """Every complete period of each declared grain lying wholly inside the span, oldest first."""
    found: list[CalendarPeriod] = []
    for name in grains:
        grain = _PERIOD_GRAINS[name]
        period = period_containing(grain, first_day)
        if period.start < first_day:
            period = period_containing(grain, period.end + timedelta(days=1))
        while period.end <= last_day:
            found.append(period)
            period = period_containing(grain, period.end + timedelta(days=1))
    return tuple(sorted(found, key=lambda item: (item.start, item.end)))


def period_window(period: CalendarPeriod, timestamp_format: str) -> tuple[str, str]:
    """Render a period's half-open AMC window, `[start, day after end)`, in the live format."""
    start = datetime.combine(period.start, time(), tzinfo=UTC)
    end = datetime.combine(period.end + timedelta(days=1), time(), tzinfo=UTC)
    return start.strftime(timestamp_format), end.strftime(timestamp_format)


@dataclass(frozen=True, slots=True)
class _Layout:
    contract: DataContract
    pack: GovernedQueryPack
    shape: SuppressionOutput
    header: tuple[str, ...]
    catalog_share: Decimal


def _rows(
    layout: _Layout,
    corpus: PeriodicCorpus,
    catalog: SyntheticEntityCatalog,
) -> Iterator[dict[str, Any]]:
    bound = BUYER_BOUNDS[layout.contract.contract_id]
    buyers = catalog.shape.new_to_brand_buyers
    span = catalog.shape.fixture_span
    policy = layout.pack.period_policy
    if policy is None:  # pragma: no cover — build_periodic is only called for periodic packs
        raise ValueError(f"{layout.pack.pack_id}: not a periodic pack.")
    marketplace = catalog.marketplace_aliases["amc.marketplace_name"]
    grains = tuple(grain.value for grain in policy.grains)
    for period in complete_periods(grains, span.first_day, span.last_day):
        start, end = period_window(period, layout.shape.timestamp_format)
        window: dict[str, Any] = {"time_window_start": start, "time_window_end": end}
        weekly = period.grain is TimeGrain.WEEK
        if weekly and period.start == buyers.not_returned_week:
            # One shopper bought in this week, below the bar of 2: AMC nulls the LOW
            # marketplace column, keeps the merged counts, and flags the row (amc.md §5).
            yield _row(layout, window | _suppressed(layout, bound))
            continue
        if bound.split_column is None:
            yield _row(layout, window | _kept(layout, corpus, (period, marketplace)))
            continue
        values = corpus.split_values(bound, (period, marketplace))
        dropped = _dropped(layout.contract.contract_id, catalog, (period, values))
        first_dropped = next((item for item in values if item in dropped), None)
        for value in values:
            if value in dropped:
                # Too few shoppers bought after this key's ads in the period: AMC returns the
                # row suppressed with its LOW keys NULL, so it never reaches the fact. A
                # period's dropped keys are one merged row here ([unverified]; see the module
                # docstring for the contrary live evidence and why the load reads both alike).
                if value == first_dropped:
                    yield _row(layout, window | _suppressed(layout, bound))
                continue
            yield _row(
                layout,
                window
                | _kept(layout, corpus, (period, marketplace, value))
                | {bound.split_column: value}
                | corpus.carried(bound, value),
            )


def _dropped(
    contract_id: str,
    catalog: SyntheticEntityCatalog,
    split: tuple[CalendarPeriod, tuple[str, ...]],
) -> frozenset[str]:
    """Return the split values a shaped period drops, refusing a declared one it lacks.

    `split` is the period and the split values with a credited purchase in it.
    """
    period, values = split
    if contract_id == "amc-new-to-brand-buyers-channel":
        channel = catalog.shape.new_to_brand_buyers_channel
        if period.grain is not TimeGrain.WEEK or period.start != channel.missing_week:
            return frozenset()
        declared = channel.missing_ad_product
        if declared not in values:
            raise FixtureError(
                f"{contract_id}: {declared} has no credited purchase in the week of "
                f"{period.start}, so it can't be the declared missing ad product."
            )
        return frozenset({declared})
    if contract_id == "amc-new-to-brand-buyers-campaign":
        shape = catalog.shape.new_to_brand_buyers_campaign
        if period.start == shape.withdrawn_month:
            count = int(
                (shape.missing_share * len(values)).to_integral_value(rounding=ROUND_CEILING)
            )
            return frozenset(values[:count])
        if period.start == shape.missing_month:
            campaign = next(
                (item for item in catalog.campaigns if item.key == shape.missing_campaign), None
            )
            alias = None if campaign is None else next(iter(campaign.source_aliases.values()))
            if alias is None or alias not in values:
                raise FixtureError(
                    f"{contract_id}: {shape.missing_campaign} has no credited purchase in "
                    f"{period.start:%B %Y}, so it can't be the declared missing campaign."
                )
            return frozenset({alias})
    return frozenset()


def _row(layout: _Layout, values: Mapping[str, Any]) -> dict[str, Any]:
    return {name: values[name] for name in layout.header}


def _suppressed(layout: _Layout, bound: BuyerBound) -> dict[str, Any]:
    """One shopper's row below the bar of 2, its LOW keys NULL and its counts kept."""
    return {
        bound.marketplace_target: None,
        **({} if bound.split_column is None else {bound.split_column: None}),
        **dict.fromkeys(bound.carried_columns),
        "buyers_new_to_brand_12mo": 1,
        "buyers_attributed": 1,
        "distinct_user_count": 1,
        "suppression_reason": layout.shape.reason(
            users=1,
            table=layout.contract.source_tables[0],
            bar=layout.pack.expected_bar,
        ),
        "rows_suppressed": True,
    }


def _kept(
    layout: _Layout,
    corpus: PeriodicCorpus,
    request: tuple[CalendarPeriod, str] | tuple[CalendarPeriod, str, str],
) -> dict[str, Any]:
    """Build a kept row: buyers_per_purchase times the matching credited purchases, rounded down."""
    bound = BUYER_BOUNDS[layout.contract.contract_id]
    share = layout.catalog_share
    return {
        bound.marketplace_target: request[1],
        **{
            target: int(
                (share * corpus.total(bound, source, request)).to_integral_value(
                    rounding=ROUND_FLOOR
                )
            )
            for target, source in bound.pairs
        },
        "distinct_user_count": layout.pack.expected_bar,
        "suppression_reason": None,
        "rows_suppressed": False,
    }


def build_periodic(
    pack: GovernedQueryPack,
    sources: tuple[DataContract, SuppressionOutput, FixtureBuildOptions],
    corpus: PeriodicCorpus,
) -> Fixture:
    """Build a periodic pack's fixture: one row per complete period, oldest first."""
    contract, shape, options = sources
    catalog = options.entity_catalog
    if catalog is None:
        raise FixtureError(f"{contract.contract_id}: a periodic fixture needs the entity catalog.")
    trailer = shape.trailing_order
    header = (*(name for name in pack.expected_columns if name not in trailer), *trailer)
    return Fixture(
        contract_id=contract.contract_id,
        source_format=contract.source_format,
        manifest=declaration_for(contract, options),
        rows=tuple(
            _rows(
                _Layout(
                    contract,
                    pack,
                    shape,
                    header,
                    catalog.shape.new_to_brand_buyers.buyers_per_purchase,
                ),
                corpus,
                catalog,
            )
        ),
    )
