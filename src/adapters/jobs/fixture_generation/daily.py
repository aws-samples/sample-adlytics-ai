# SPDX-License-Identifier: MIT-0
"""Lay a daily distinct-count pack's fixture out as one delivery per UTC day (pack 4).

`amc-dsp-reach` counts distinct shoppers per campaign per day, and its `reject_delivery` policy
refuses a whole delivery for one suppressed row. So its fixture is one delivery per declared day,
each with the AMC window `[day, day + 1)`, and a refused day refuses only itself.
`land_fixtures` splits the file back into one delivery per window.

No value is random. Each row is the campaign-day the generator already declares for every
DSP contract, kept only where the same day's `amc-dsp-impressions` corpus has impressions (the
pack filters `impressions > 0`), so reach never exceeds impressions: a shopper reached was shown
at least one impression. The declared suppressed campaign-day is the one shopper AMC returns
suppressed with its LOW keys NULL (`suppression_output` in `contracts/shared/amc-columns.yaml`);
its `reach_users` of 1 beside them is [unverified], since that observation records no distinct
count column.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

from adapters.jobs.fixture_generation.shapes import Fixture, FixtureError

if TYPE_CHECKING:  # pragma: no cover — typing only
    from adapters.jobs.fixture_generation.catalog import SyntheticEntityCatalog
    from adapters.jobs.fixture_generation.conformance import SuppressionOutput
    from domain.data_contracts.contract import DataContract
    from domain.producer.models import GovernedQueryPack

__all__ = ["DAILY_BOUNDS", "DailyBound", "DailyInputs", "build_daily", "day_window"]


@dataclass(frozen=True, slots=True)
class DailyBound:
    """Which corpus bounds one daily distinct-count pack, and on which columns.

    `pairs` maps each distinct-count column to the corpus column it can't exceed on the same
    day, campaign and marketplace.
    """

    corpus_contract: str
    date_column: str
    campaign_column: str
    marketplace_column: str
    pairs: tuple[tuple[str, str], ...]


DAILY_BOUNDS: Mapping[str, DailyBound] = {
    "amc-dsp-reach": DailyBound(
        corpus_contract="amc-dsp-impressions",
        date_column="impression_date_utc",
        campaign_column="campaign_id_string",
        marketplace_column="campaign_country",
        pairs=(("reach_users", "impressions"),),
    ),
}
"""The one daily-delivery pack and the corpus that bounds it; the conformance check reads it."""

_DSP_ALIAS = "amc.dsp.campaign_id_string"


def day_window(day: date, timestamp_format: str) -> tuple[str, str]:
    """Render one UTC day's half-open AMC window, `[day, day + 1)`, in the live format."""
    start = datetime.combine(day, time(), tzinfo=UTC)
    return start.strftime(timestamp_format), (start + timedelta(days=1)).strftime(timestamp_format)


def _impressions(
    bound: DailyBound, corpus: tuple[Mapping[str, Any], ...]
) -> dict[tuple[str, str, str], int]:
    totals: dict[tuple[str, str, str], int] = {}
    for row in corpus:
        key = (
            str(row[bound.date_column]),
            str(row[bound.campaign_column]),
            str(row[bound.marketplace_column]),
        )
        for _target, source in bound.pairs:
            totals[key] = totals.get(key, 0) + int(row[source] or 0)
    return totals


@dataclass(frozen=True, slots=True)
class DailyInputs:
    """What one daily-delivery fixture is laid out from."""

    contract: DataContract
    pack: GovernedQueryPack
    shape: SuppressionOutput
    corpus: tuple[Mapping[str, Any], ...]
    """The bounding corpus's generated rows (`DailyBound.corpus_contract`)."""
    catalog: SyntheticEntityCatalog


def build_daily(fixture: Fixture, inputs: DailyInputs) -> Fixture:
    """Keep the declared days' delivered campaign-days, each stamped with its own day's window."""
    contract, pack, shape = inputs.contract, inputs.pack, inputs.shape
    corpus, catalog = inputs.corpus, inputs.catalog
    bound = DAILY_BOUNDS[contract.contract_id]
    declared = catalog.shape.dsp_reach
    campaign = next(
        (item for item in catalog.campaigns if item.key == declared.suppressed_campaign), None
    )
    if campaign is None or _DSP_ALIAS not in campaign.source_aliases:
        raise FixtureError(f"{contract.contract_id}: the suppressed campaign must be a DSP one.")
    suppressed = (declared.suppressed_day.isoformat(), campaign.source_aliases[_DSP_ALIAS])
    impressions = _impressions(bound, corpus)
    days = {day.isoformat() for day in declared.days}
    rows: list[dict[str, Any]] = []
    found = False
    for row in fixture.rows:
        day = str(row[bound.date_column])
        key = (day, str(row[bound.campaign_column]), str(row[bound.marketplace_column]))
        if day not in days or impressions.get(key, 0) < 1:
            continue
        start, end = day_window(date.fromisoformat(day), shape.timestamp_format)
        values = {**row, "time_window_start": start, "time_window_end": end}
        if key[:2] == suppressed:
            found = True
            # One shopper, below the bar of 2: AMC nulls the LOW keys and flags the row
            # (docs/sources/amc.md section 5). That the distinct count itself survives is
            # [unverified]: the observed suppressed shape records measures and CTE-derived
            # labels, not a COUNT(DISTINCT user_id) column. Under reject_delivery the delivery
            # is refused whole either way.
            values |= {
                bound.date_column: None,
                bound.campaign_column: None,
                bound.marketplace_column: None,
                **{target: 1 for target, _source in bound.pairs},
                "distinct_user_count": 1,
                "suppression_reason": shape.reason(
                    users=1, table=contract.source_tables[0], bar=pack.expected_bar
                ),
                "rows_suppressed": True,
            }
        rows.append(values)
    if not found:
        raise FixtureError(
            f"{contract.contract_id}: {declared.suppressed_campaign} delivered no impressions on "
            f"{declared.suppressed_day}, so it can't be the suppressed campaign-day."
        )
    return replace(fixture, rows=tuple(rows))
