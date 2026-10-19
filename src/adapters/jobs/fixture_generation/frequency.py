# SPDX-License-Identifier: MIT-0
"""Lay the frequency fixtures (packs 6 and 7) out as one delivery of every bucket per period.

Packs 6 and 7 (`amc-exposure-frequency-platform` and `-channel`, design §6.3 D2) are executed
once per complete calendar month and quarter. Each delivery is every bucket of its scheme by two
identity spaces, pack 7's per ad product. No value is random: a period's DSP and sponsored
impressions and raw costs are the `amc-dsp-impressions` and `amc-reach-frequency` corpora's over
its days (pack 7: per ad product, DSP as `amazon_dsp`), split by the declared shares
(`ExposureFrequencyShape`); a single-count bucket's impressions are exactly its shoppers times
its count, so the counts are consistent by construction.

Every month after the declared withheld month is delivered under the coarse scheme, because that
month's degraded slice switched it on (plan D10, sticky); quarters stay fine. The withheld row
mirrors the live multi-source suppression shape pack 5's fixture uses: it keeps its derived
bucket, its distinct counts and exposure-side sums, nulls its identity space, marketplace, ad
product, purchase-side sums and every currency, and names one source path in a `(path i of n)`
reason.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from typing import TYPE_CHECKING, Any

from adapters.jobs.fixture_generation.exposure import ExposureCorpus, PathReason
from adapters.jobs.fixture_generation.periodic import complete_periods, period_window
from adapters.jobs.fixture_generation.shapes import Fixture, FixtureError, declaration_for
from domain.semantics.time import TimeGrain

if TYPE_CHECKING:  # pragma: no cover — typing only
    from adapters.jobs.fixture_generation.catalog import SyntheticEntityCatalog
    from adapters.jobs.fixture_generation.conformance import SuppressionOutput
    from adapters.jobs.fixture_generation.shape import ExposureFrequencyShape
    from adapters.jobs.fixture_generation.shapes import FixtureBuildOptions
    from domain.data_contracts.contract import DataContract
    from domain.producer.models import GovernedQueryPack
    from domain.semantics.calendar_periods import CalendarPeriod

__all__ = [
    "COARSE_BUCKETS",
    "FINE_BUCKETS",
    "FREQUENCY_CHANNEL",
    "FREQUENCY_CONTRACTS",
    "FREQUENCY_PLATFORM",
    "build_exposure_frequency",
    "coarse_from",
]

FREQUENCY_PLATFORM = "amc-exposure-frequency-platform"
FREQUENCY_CHANNEL = "amc-exposure-frequency-channel"
FREQUENCY_CONTRACTS = (FREQUENCY_PLATFORM, FREQUENCY_CHANNEL)
FINE_BUCKETS = ("1", "2", "3", "4", "5", "6", "7", "8", "9", "10_plus")
COARSE_BUCKETS = ("1", "2", "3", "4_6", "7_9", "10_plus")
_MERGED = {"4_6": ("4", "5", "6"), "7_9": ("7", "8", "9")}
_IDENTITY_SPACES = ("account", "device_or_browser")
_DSP = "amazon_dsp"
_SPONSORED_PATH = 2
"""`sponsored_ads_traffic` is the contract's second source table, the path the tail bucket lacks."""


def coarse_from(shape: ExposureFrequencyShape) -> date:
    """Return the first month delivered under the coarse scheme, the one after the degraded."""
    month = shape.withheld_month
    return date(month.year + month.month // 12, month.month % 12 + 1, 1)


def _floor(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_FLOOR))


@dataclass(frozen=True, slots=True)
class _Touches:
    """One slice's impressions, raw DSP cost and raw sponsored spend over a period."""

    impressions: int
    dsp_cost: int
    sponsored_spend: int


def _touches(corpus: ExposureCorpus, period: CalendarPeriod) -> dict[str | None, _Touches]:
    """Each ad product's touches in the period, and the platform's under None."""

    def within(row: Mapping[str, Any], column: str) -> bool:
        return period.start <= date.fromisoformat(str(row[column])) <= period.end

    found: dict[str | None, list[int]] = {}
    for row in corpus.dsp:
        if within(row, "impression_date_utc"):
            totals = found.setdefault(_DSP, [0, 0, 0])
            totals[0] += int(row["impressions"])
            totals[1] += int(row["total_cost"])
    for row in corpus.sponsored:
        if within(row, "event_date_utc"):
            totals = found.setdefault(str(row["ad_product_type"]), [0, 0, 0])
            totals[0] += int(row["impressions"])
            totals[2] += int(row["spend"])
    platform = [sum(values[index] for values in found.values()) for index in range(3)]
    return {
        None: _Touches(*platform),
        **{key: _Touches(*values) for key, values in sorted(found.items(), key=str)},
    }


@dataclass(frozen=True, slots=True)
class _Layout:
    contract: DataContract
    pack: GovernedQueryPack
    shape: SuppressionOutput
    header: tuple[str, ...]
    frequency: ExposureFrequencyShape
    marketplace: str
    currency: str


def _impressions_per(shape: ExposureFrequencyShape) -> dict[str, int]:
    return {
        bucket: shape.ten_plus_impressions if bucket == "10_plus" else int(bucket)
        for bucket in FINE_BUCKETS
    }


def _fine_cells(layout: _Layout, touches: _Touches, space: str) -> dict[str, dict[str, int]]:
    """One identity space's fine buckets: shoppers, impressions, raw spends and outcomes."""
    shape = layout.frequency
    per = _impressions_per(shape)
    share = shape.account_share if space == "account" else 1 - shape.account_share
    mean = sum(
        (
            bucket_share * per[bucket]
            for bucket, bucket_share in zip(FINE_BUCKETS, shape.bucket_shares, strict=True)
        ),
        Decimal(0),
    )
    total = _floor(Decimal(touches.impressions) * share / mean)
    factor = Decimal(1) if space == "account" else shape.device_rate_factor
    cells: dict[str, dict[str, int]] = {}
    for bucket, bucket_share, rate in zip(
        FINE_BUCKETS, shape.bucket_shares, shape.purchaser_rates, strict=True
    ):
        shoppers = _floor(total * bucket_share)
        impressions = shoppers * per[bucket]
        purchasers = _floor(shoppers * rate * factor)
        cells[bucket] = {
            "shoppers": shoppers,
            "impressions": impressions,
            "dsp": touches.dsp_cost * impressions // touches.impressions,
            "sponsored": touches.sponsored_spend * impressions // touches.impressions,
            "purchasers": purchasers,
            "ntb": _floor(purchasers * shape.ntb_share),
            "purchases": _floor(purchasers * shape.purchases_per_purchaser),
        }
    return cells


def _scheme_cells(fine: dict[str, dict[str, int]], scheme: str) -> dict[str, dict[str, int]]:
    """Return the scheme's buckets; a coarse range bucket sums its fine ones (disjoint)."""
    if scheme == "fine":
        return fine
    return {
        bucket: {
            key: sum(fine[part][key] for part in _MERGED.get(bucket, (bucket,)))
            for key in fine["1"]
        }
        for bucket in COARSE_BUCKETS
    }


def _kept(
    layout: _Layout, slot: tuple[str, str, str, str | None], cell: dict[str, int]
) -> dict[str, Any]:
    bucket, space, scheme, ad_product = slot
    currency = layout.currency
    dsp = currency if cell["dsp"] else None
    sponsored = currency if cell["sponsored"] else None
    purchase = currency if cell["purchases"] else None
    return {
        "ad_product_type": ad_product,
        "frequency_bucket": bucket,
        "identity_space": space,
        "bucket_scheme": scheme,
        "row_kind": "window",
        "shoppers": cell["shoppers"],
        "impressions": cell["impressions"],
        "dsp_window_spend": cell["dsp"],
        "sponsored_window_spend": cell["sponsored"],
        "dsp_currency": dsp,
        "dsp_currency_min": dsp,
        "sponsored_currency": sponsored,
        "sponsored_currency_min": sponsored,
        "purchasers": cell["purchasers"],
        "ntb_purchasers": cell["ntb"],
        "purchases": cell["purchases"],
        "revenue_incl_halo": f"{layout.frequency.order_value * cell['purchases']:.2f}",
        "currency_code": purchase,
        "purchase_currency_min": purchase,
        "marketplace_name": layout.marketplace,
        "distinct_user_count": layout.pack.expected_bar,
        "suppression_reason": None,
        "rows_suppressed": False,
    }


def _withheld(layout: _Layout, kept: dict[str, Any]) -> dict[str, Any]:
    """Withhold a tail bucket the live multi-source way, naming the short sponsored path."""
    shoppers = layout.frequency.withheld_shoppers
    tables = layout.contract.source_tables
    return {
        **kept,
        "ad_product_type": None,
        "identity_space": None,
        "marketplace_name": None,
        "shoppers": shoppers,
        "impressions": shoppers * layout.frequency.ten_plus_impressions,
        "purchasers": shoppers - 1,
        "ntb_purchasers": 1,
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
            1, tables[_SPONSORED_PATH - 1], _SPONSORED_PATH, len(tables), layout.pack.expected_bar
        ).text,
        "rows_suppressed": True,
    }


def _rows(
    layout: _Layout, corpus: ExposureCorpus, catalog: SyntheticEntityCatalog
) -> Iterator[dict[str, Any]]:
    policy = layout.pack.period_policy
    if policy is None:  # pragma: no cover — packs 6 and 7 are periodic
        raise FixtureError(f"{layout.pack.pack_id}: not a periodic pack.")
    span = catalog.shape.fixture_span
    shape = layout.frequency
    channel = layout.contract.contract_id == FREQUENCY_CHANNEL
    for period in complete_periods(
        tuple(grain.value for grain in policy.grains), span.first_day, span.last_day
    ):
        start, end = period_window(period, layout.shape.timestamp_format)
        window = {"time_window_start": start, "time_window_end": end}
        monthly = period.grain is TimeGrain.MONTH
        # The switch needs its degraded month delivered: a shortened horizon that starts after
        # it holds no switch, so every month stays fine.
        switched = shape.withheld_month >= span.first_day and period.start >= coarse_from(shape)
        scheme = "coarse" if monthly and switched else "fine"
        slices = _touches(corpus, period)
        products = tuple(key for key in slices if key is not None) if channel else (None,)
        for ad_product in products:
            touches = slices[ad_product]
            if touches.impressions == 0:
                continue
            for space in _IDENTITY_SPACES:
                cells = _scheme_cells(_fine_cells(layout, touches, space), scheme)
                for bucket, cell in cells.items():
                    kept = _kept(layout, (bucket, space, scheme, ad_product), cell)
                    shaped = monthly and (not channel or ad_product == shape.withheld_ad_product)
                    if (
                        shaped
                        and period.start == shape.withheld_month
                        and (bucket, space) == (shape.withheld_bucket, shape.withheld_space)
                    ):
                        # Three shoppers, but fewer than two through the sponsored path: withheld
                        # per path and discarded like any withheld row (plan D19).
                        yield _row(layout, window | _withheld(layout, kept))
                        continue
                    if (
                        shaped
                        and period.start == shape.null_bucket_month
                        and (bucket, space) == ("1", "account")
                    ):
                        # A shopper outside the fence: the CASE's missing ELSE yields NULL, and
                        # the load refuses the delivery (FREQUENCY_BUCKET_NULL).
                        kept = kept | {"frequency_bucket": None}
                    yield _row(layout, window | kept)


def _row(layout: _Layout, values: Mapping[str, Any]) -> dict[str, Any]:
    return {name: values[name] for name in layout.header}


def build_exposure_frequency(
    pack: GovernedQueryPack,
    sources: tuple[DataContract, SuppressionOutput, FixtureBuildOptions],
    corpus: ExposureCorpus,
) -> Fixture:
    """Build pack 6's or pack 7's fixture: every bucket per complete month and quarter."""
    contract, shape, options = sources
    catalog = options.entity_catalog
    if catalog is None:
        raise FixtureError(f"{contract.contract_id}: a periodic fixture needs the entity catalog.")
    trailer = shape.trailing_order
    header = (*(name for name in pack.expected_columns if name not in trailer), *trailer)
    currencies = {str(row["currency_iso_code"]) for row in (*corpus.dsp, *corpus.sponsored)}
    if len(currencies) != 1:
        raise FixtureError(f"{contract.contract_id}: the touch corpora carry {currencies}.")
    layout = _Layout(
        contract,
        pack,
        shape,
        header,
        catalog.shape.exposure_frequency,
        catalog.marketplace_aliases["amc.marketplace_name"],
        currencies.pop(),
    )
    return Fixture(
        contract_id=contract.contract_id,
        source_format=contract.source_format,
        manifest=declaration_for(contract, options),
        rows=tuple(_rows(layout, corpus, catalog)),
    )
