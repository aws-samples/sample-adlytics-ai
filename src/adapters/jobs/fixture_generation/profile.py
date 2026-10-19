# SPDX-License-Identifier: MIT-0
"""Lay the buyer-profile fixtures (packs 10-12) out as one delivery per complete month.

Each pack returns one row per declared segment and one `profiled_total` row (design §5.5): pack
10 the in-market ASI segments, pack 11 the lifestyle ones, pack 12 the DSP behaviour segments
(`SegmentProfileShape`). No value is random: every count is a declared share of the declared
profiled base, so a segment's buyers never exceed its exposed shoppers and the total rows hold
the bases the shares read.

Three months are shaped. Before `asi_from` packs 10-11 deliver nothing (ASI absent). In
`total_withheld` one pack's total row comes back withheld (its marketplace NULL, its bases the one
user under the bar), so the load writes it not_returned. In `segment_withheld` one segment
row comes back withheld the live multi-source way with two or more shoppers: its segment ID and
marketplace NULL and a `(path i of n)` reason naming the conversions path (plan D19).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from typing import TYPE_CHECKING, Any

from adapters.jobs.fixture_generation.exposure import PathReason
from adapters.jobs.fixture_generation.periodic import complete_periods, period_window
from adapters.jobs.fixture_generation.shapes import Fixture, FixtureError, declaration_for

if TYPE_CHECKING:  # pragma: no cover — typing only
    from adapters.jobs.fixture_generation.conformance import SuppressionOutput
    from adapters.jobs.fixture_generation.shape import ProfileSource, SegmentProfileShape
    from adapters.jobs.fixture_generation.shapes import FixtureBuildOptions
    from domain.data_contracts.contract import DataContract
    from domain.producer.models import GovernedQueryPack
    from domain.semantics.calendar_periods import CalendarPeriod

__all__ = ["PROFILE_CONTRACTS", "build_segment_profile"]

PROFILE_CONTRACTS = (
    "amc-segment-buyer-profile-inmarket",
    "amc-segment-buyer-profile-lifestyle",
    "amc-segment-buyer-profile-dsp-exposed",
)
_PAID = frozenset(PROFILE_CONTRACTS[:2])


def _floor(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_FLOOR))


@dataclass(frozen=True, slots=True)
class _Layout:
    contract: DataContract
    pack: GovernedQueryPack
    shape: SuppressionOutput
    header: tuple[str, ...]
    profile: SegmentProfileShape
    source: ProfileSource
    marketplace: str

    @property
    def segment_column(self) -> str:
        """Pack 12 returns `behavior_segment_id`; packs 10-11 `segment_id`."""
        return next(name for name in self.header if name.endswith("segment_id"))


def _clean(layout: _Layout) -> dict[str, Any]:
    return {
        "marketplace_name": layout.marketplace,
        "distinct_user_count": layout.pack.expected_bar,
        "suppression_reason": None,
        "rows_suppressed": False,
    }


def _bases(profile: SegmentProfileShape) -> tuple[int, int]:
    """Return the profiled exposed shoppers and the profiled buyers among them."""
    return profile.profiled_exposed, _floor(profile.profiled_exposed * profile.buyer_rate)


def _segment(layout: _Layout, index: int) -> dict[str, Any]:
    exposed_total, buyers_total = _bases(layout.profile)
    source = layout.source
    buyers = _floor(buyers_total * source.buyer_shares[index])
    exposed = max(_floor(exposed_total * source.exposed_shares[index]), buyers)
    return {
        "row_kind": "segment",
        layout.segment_column: source.segments[index],
        "segment_exposed_shoppers": exposed,
        "segment_buyers": buyers,
        "segment_buyers_new_to_brand": _floor(buyers * layout.profile.ntb_share),
        "profiled_exposed_total": None,
        "profiled_buyers_total": None,
        **_clean(layout),
    }


def _total(layout: _Layout) -> dict[str, Any]:
    exposed_total, buyers_total = _bases(layout.profile)
    return {
        "row_kind": "profiled_total",
        layout.segment_column: None,
        "segment_exposed_shoppers": None,
        "segment_buyers": None,
        "segment_buyers_new_to_brand": None,
        "profiled_exposed_total": exposed_total,
        "profiled_buyers_total": buyers_total,
        **_clean(layout),
    }


def _withheld(layout: _Layout, kept: dict[str, Any], users: tuple[int, int]) -> dict[str, Any]:
    """Withhold a row the live multi-source way: its keys NULL, one path short.

    `users` is the row's withheld shoppers, which its counts become (never more buyers than
    shoppers), and the 1-based path in `source_tables` whose count fell under the bar.
    """
    shoppers, path = users
    tables = layout.contract.source_tables
    counts = (
        {
            # A withheld total holds only the users it was withheld for, never the month's bases.
            "profiled_exposed_total": shoppers,
            "profiled_buyers_total": min(kept["profiled_buyers_total"], shoppers),
        }
        if kept["row_kind"] != "segment"
        else {
            "segment_exposed_shoppers": shoppers,
            "segment_buyers": shoppers,
            # Never more new-to-brand buyers than buyers: a shape AMC can't return.
            "segment_buyers_new_to_brand": min(kept["segment_buyers_new_to_brand"], shoppers),
        }
    )
    return {
        **kept,
        **counts,
        layout.segment_column: None,
        "marketplace_name": None,
        "distinct_user_count": 1,
        "suppression_reason": PathReason(
            1, tables[path - 1], path, len(tables), layout.pack.expected_bar
        ).text,
        "rows_suppressed": True,
    }


def _month_rows(layout: _Layout, period: CalendarPeriod) -> Iterator[dict[str, Any]]:
    profile = layout.profile
    source = layout.source.source
    conversions = len(layout.contract.source_tables)
    withheld = {
        segment: shoppers
        for month, owner, segment, shoppers in profile.segments_withheld
        if (month, owner) == (period.start, source)
    }
    for index, segment in enumerate(layout.source.segments):
        kept = _segment(layout, index)
        if segment in withheld:
            # Two or more shoppers, but fewer than two of them bought through conversions: the
            # row is withheld per path and discarded like any withheld row (plan D19).
            yield _withheld(layout, kept, (withheld[segment], conversions))
            continue
        yield kept
    total = _total(layout)
    if (period.start, source) in profile.totals_withheld:
        # One user under the bar on the conversions path: the row holds that user, not the bases.
        yield _withheld(layout, total, (1, conversions))
        return
    yield total


def _rows(layout: _Layout, options: FixtureBuildOptions) -> Iterator[dict[str, Any]]:
    policy = layout.pack.period_policy
    catalog = options.entity_catalog
    if policy is None or catalog is None:  # pragma: no cover — packs 10-12 are periodic
        raise FixtureError(f"{layout.pack.pack_id}: not a periodic pack with a catalog.")
    span = catalog.shape.fixture_span
    for period in complete_periods(
        tuple(grain.value for grain in policy.grains), span.first_day, span.last_day
    ):
        if layout.contract.contract_id in _PAID and period.start < layout.profile.asi_from:
            continue
        start, end = period_window(period, layout.shape.timestamp_format)
        window = {"time_window_start": start, "time_window_end": end}
        for values in _month_rows(layout, period):
            yield {name: (window | values)[name] for name in layout.header}


def _source_of(contract: DataContract) -> str:
    field = contract.field_for("profile_path")
    if field is None or field.literal_value is None:
        raise FixtureError(f"{contract.contract_id}: declares no profile_path literal.")
    return str(field.literal_value)


def build_segment_profile(
    pack: GovernedQueryPack,
    sources: tuple[DataContract, SuppressionOutput, FixtureBuildOptions],
) -> Fixture:
    """Build pack 10's, 11's or 12's fixture: one delivery per complete calendar month."""
    contract, shape, options = sources
    catalog = options.entity_catalog
    if catalog is None:
        raise FixtureError(f"{contract.contract_id}: a periodic fixture needs the entity catalog.")
    trailer = shape.trailing_order
    header = (*(name for name in pack.expected_columns if name not in trailer), *trailer)
    profile = catalog.shape.segment_profile
    layout = _Layout(
        contract,
        pack,
        shape,
        header,
        profile,
        profile.source(_source_of(contract)),
        catalog.marketplace_aliases["amc.marketplace_name"],
    )
    return Fixture(
        contract_id=contract.contract_id,
        source_format=contract.source_format,
        manifest=declaration_for(contract, options),
        rows=tuple(_rows(layout, options)),
    )
