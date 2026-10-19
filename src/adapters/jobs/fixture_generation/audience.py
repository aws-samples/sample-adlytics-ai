# SPDX-License-Identifier: MIT-0
"""Lay the audience fixtures (packs 9, 13 and 8) out as one delivery per complete month.

Pack 9 (`amc-dsp-segment-names`) returns each declared DSP segment's name and description, pack
13 (`amc-segment-metadata`) each declared ASI segment's metadata under the pinned
`segment_marketplace_id`, and pack 8 (`amc-audience-performance`) one row per DSP campaign with
impressions in the month and DSP segment (`AudienceSegmentsShape`). No value is random: a
campaign's month impressions and raw cost are the `amc-dsp-impressions` corpus's over its days.

Two months are shaped. In `name_withheld` the segment's pack 9 row comes back withheld (its LOW
keys and text NULL, one user), so it is dropped and the name arrives the month after. In
`roster_gap` one campaign's pack 8 row for the segment comes back withheld the live multi-source
way: identity keys, purchase-side sums and currencies NULL, a `(path i of n)` reason naming the
attributed-events path.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from typing import TYPE_CHECKING, Any

from adapters.jobs.fixture_generation.exposure import PathReason
from adapters.jobs.fixture_generation.periodic import complete_periods, period_window
from adapters.jobs.fixture_generation.shapes import Fixture, FixtureError, declaration_for

if TYPE_CHECKING:  # pragma: no cover — typing only
    from adapters.jobs.fixture_generation.catalog import SyntheticEntityCatalog
    from adapters.jobs.fixture_generation.conformance import SuppressionOutput
    from adapters.jobs.fixture_generation.shape import AudienceSegmentsShape
    from adapters.jobs.fixture_generation.shapes import FixtureBuildOptions
    from domain.data_contracts.contract import DataContract
    from domain.producer.models import GovernedQueryPack
    from domain.semantics.calendar_periods import CalendarPeriod

__all__ = [
    "AUDIENCE_CONTRACTS",
    "AUDIENCE_PERFORMANCE",
    "DSP_SEGMENT_NAMES",
    "SEGMENT_METADATA",
    "build_audience",
]

DSP_SEGMENT_NAMES = "amc-dsp-segment-names"
SEGMENT_METADATA = "amc-segment-metadata"
AUDIENCE_PERFORMANCE = "amc-audience-performance"
AUDIENCE_CONTRACTS = (DSP_SEGMENT_NAMES, SEGMENT_METADATA, AUDIENCE_PERFORMANCE)
_OUTCOME_PATH = 2
"""`amazon_attributed_events_by_conversion_time` is pack 8's second source table."""


def _floor(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_FLOOR))


@dataclass(frozen=True, slots=True)
class _Layout:
    contract: DataContract
    pack: GovernedQueryPack
    shape: SuppressionOutput
    header: tuple[str, ...]
    audience: AudienceSegmentsShape
    country: str
    segment_marketplace: str
    roster_gap_campaign: str


def _row(layout: _Layout, values: Mapping[str, Any]) -> dict[str, Any]:
    return {name: values[name] for name in layout.header}


def _clean(layout: _Layout) -> dict[str, Any]:
    return {
        "distinct_user_count": layout.pack.expected_bar,
        "suppression_reason": None,
        "rows_suppressed": False,
    }


def _withheld_single(layout: _Layout, keys: tuple[str, ...]) -> dict[str, Any]:
    """One shopper's row below the bar: its LOW keys and text NULL (single source path)."""
    return {
        **dict.fromkeys(keys),
        "distinct_user_count": 1,
        "suppression_reason": layout.shape.reason(
            users=1, table=layout.contract.source_tables[0], bar=layout.pack.expected_bar
        ),
        "rows_suppressed": True,
    }


def _matched(audience: AudienceSegmentsShape, segment_id: str, period: CalendarPeriod) -> bool:
    """Whether a DSP segment matched impressions in the period: the late segment from its month."""
    return (
        segment_id != audience.name_withheld_segment or period.start >= audience.name_withheld_month
    )


def _names(layout: _Layout, period: CalendarPeriod) -> Iterator[dict[str, Any]]:
    audience = layout.audience
    for segment in audience.dsp_segments:
        if not _matched(audience, segment.segment_id, period):
            continue
        if (period.start, segment.segment_id) == (
            audience.name_withheld_month,
            audience.name_withheld_segment,
        ):
            yield _withheld_single(
                layout,
                ("campaign_country", "behavior_segment_id", "segment_name", "segment_description"),
            )
            continue
        yield {
            "campaign_country": layout.country,
            "behavior_segment_id": segment.segment_id,
            "segment_name": segment.name,
            "segment_description": segment.description,
            **_clean(layout),
        }


def _metadata(layout: _Layout) -> Iterator[dict[str, Any]]:
    for segment in layout.audience.asi_segments:
        if segment.categories is None:  # pragma: no cover — the shape requires them on ASI
            raise FixtureError(f"{SEGMENT_METADATA}: ASI segment {segment.segment_id} lacks them.")
        level_1, level_2, category_path = segment.categories
        yield {
            "segment_marketplace_id": layout.segment_marketplace,
            "segment_id": segment.segment_id,
            "segment_name": segment.name,
            "segment_description": segment.description,
            "category_level_1": level_1,
            "category_level_2": level_2,
            "category_path": category_path,
            **_clean(layout),
        }


def _campaign_months(
    corpus: tuple[Mapping[str, Any], ...], period: CalendarPeriod
) -> dict[str, tuple[int, int, str]]:
    """Each DSP campaign's impressions, raw cost and currency over the period's days."""
    found: dict[str, list[int]] = {}
    currencies: dict[str, set[str]] = {}
    for row in corpus:
        if period.start <= date.fromisoformat(str(row["impression_date_utc"])) <= period.end:
            campaign = str(row["campaign_id_string"])
            totals = found.setdefault(campaign, [0, 0])
            totals[0] += int(row["impressions"])
            totals[1] += int(row["total_cost"])
            currencies.setdefault(campaign, set()).add(str(row["currency_iso_code"]))
    for campaign, seen in currencies.items():
        if len(seen) != 1:
            raise FixtureError(f"{AUDIENCE_PERFORMANCE}: campaign {campaign} has {seen}.")
    return {
        campaign: (totals[0], totals[1], next(iter(currencies[campaign])))
        for campaign, totals in sorted(found.items())
        if totals[0] > 0
    }


def _delivery(layout: _Layout, request: tuple[str, int, int, str, int]) -> dict[str, Any]:
    """One campaign and segment's kept row, its counts consistent by construction."""
    campaign, impressions, cost, currency, index = request
    audience = layout.audience
    segment = audience.dsp_segments[index]
    matched = _floor(impressions * audience.segment_shares[index])
    reach = matched // audience.impressions_per_shopper
    purchasers = _floor(reach * audience.purchaser_rate)
    purchases = _floor(purchasers * audience.purchases_per_purchaser) if purchasers else None
    return {
        "campaign_country": layout.country,
        "campaign_id_string": campaign,
        "behavior_segment_id": segment.segment_id,
        "impressions": matched,
        "dsp_spend": cost * matched // impressions,
        "dsp_currency": currency,
        "dsp_currency_min": currency,
        "segment_reach": reach,
        "segment_purchasers": purchasers,
        "segment_purchases": purchases,
        "segment_attributed_revenue": None
        if purchases is None
        else f"{audience.order_value * purchases:.2f}",
        "purchase_currency": None if purchases is None else currency,
        "purchase_currency_min": None if purchases is None else currency,
        **_clean(layout),
    }


def _withheld_path(layout: _Layout, kept: dict[str, Any]) -> dict[str, Any]:
    """Withhold a campaign's segment row the live multi-source way (plan D19)."""
    tables = layout.contract.source_tables
    return {
        **kept,
        "campaign_country": None,
        "campaign_id_string": None,
        "behavior_segment_id": None,
        "segment_reach": 3,
        "segment_purchasers": 1,
        **dict.fromkeys(
            (
                "segment_purchases",
                "segment_attributed_revenue",
                "purchase_currency",
                "purchase_currency_min",
                "dsp_currency",
                "dsp_currency_min",
            )
        ),
        "distinct_user_count": 1,
        "suppression_reason": PathReason(
            1, tables[_OUTCOME_PATH - 1], _OUTCOME_PATH, len(tables), layout.pack.expected_bar
        ).text,
        "rows_suppressed": True,
    }


def _performance(
    layout: _Layout, corpus: tuple[Mapping[str, Any], ...], period: CalendarPeriod
) -> Iterator[dict[str, Any]]:
    audience = layout.audience
    for campaign, (impressions, cost, currency) in _campaign_months(corpus, period).items():
        for index, segment in enumerate(audience.dsp_segments):
            if not _matched(audience, segment.segment_id, period):
                continue
            kept = _delivery(layout, (campaign, impressions, cost, currency, index))
            if (period.start, campaign, segment.segment_id) == (
                audience.roster_gap_month,
                layout.roster_gap_campaign,
                audience.roster_gap_segment,
            ):
                yield _withheld_path(layout, kept)
                continue
            yield kept


def _rows(
    layout: _Layout, corpus: tuple[Mapping[str, Any], ...], catalog: SyntheticEntityCatalog
) -> Iterator[dict[str, Any]]:
    policy = layout.pack.period_policy
    if policy is None:  # pragma: no cover — packs 8, 9 and 13 are periodic
        raise FixtureError(f"{layout.pack.pack_id}: not a periodic pack.")
    span = catalog.shape.fixture_span
    contract_id = layout.contract.contract_id
    for period in complete_periods(
        tuple(grain.value for grain in policy.grains), span.first_day, span.last_day
    ):
        start, end = period_window(period, layout.shape.timestamp_format)
        window = {"time_window_start": start, "time_window_end": end}
        if contract_id == DSP_SEGMENT_NAMES:
            rows = _names(layout, period)
        elif contract_id == SEGMENT_METADATA:
            rows = _metadata(layout)
        else:
            rows = _performance(layout, corpus, period)
        for values in rows:
            yield _row(layout, window | values)


def build_audience(
    pack: GovernedQueryPack,
    sources: tuple[DataContract, SuppressionOutput, FixtureBuildOptions],
    corpus: tuple[Mapping[str, Any], ...],
) -> Fixture:
    """Build pack 9's, 13's or 8's fixture: one delivery per complete calendar month."""
    contract, shape, options = sources
    catalog = options.entity_catalog
    if catalog is None:
        raise FixtureError(f"{contract.contract_id}: a periodic fixture needs the entity catalog.")
    trailer = shape.trailing_order
    header = (*(name for name in pack.expected_columns if name not in trailer), *trailer)
    gap = catalog.shape.audience_segments.roster_gap_campaign
    aliases = next((item.source_aliases for item in catalog.campaigns if item.key == gap), None)
    if aliases is None or "amc.dsp.campaign_id_string" not in aliases:
        raise FixtureError(f"{contract.contract_id}: {gap} has no DSP campaign_id_string.")
    layout = _Layout(
        contract,
        pack,
        shape,
        header,
        catalog.shape.audience_segments,
        catalog.marketplace_aliases["amc.campaign_country"],
        catalog.marketplace_aliases["amc.segment_marketplace_id"],
        aliases["amc.dsp.campaign_id_string"],
    )
    return Fixture(
        contract_id=contract.contract_id,
        source_format=contract.source_format,
        manifest=declaration_for(contract, options),
        rows=tuple(_rows(layout, corpus, catalog)),
    )
