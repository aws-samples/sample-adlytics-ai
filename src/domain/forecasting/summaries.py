# SPDX-License-Identifier: MIT-0
"""Forecast summaries for the page and tools, one implementation for both (design §4a).

Slice 2 holds only `roster_entry()`, the one function that builds a list row: `publish_shard`
uses it for roster parts and the readers for an exact-ASIN search. The p50 totals are sums of
the stored four-place values (§4d), so they are exact and stay stored values; rounding for
display happens later, once. `accuracy_class` is null until `classify_accuracy()` lands
(slice 3).
"""

from __future__ import annotations

from decimal import Decimal

from domain.forecasting.boundary import (
    CandidateSeriesV1,
    RosterEntryV1,
    RosterTargetV1,
    TargetCandidateV1,
)
from domain.forecasting.numeric import stored_text
from domain.forecasting.policy import MONEY_TARGETS, TARGETS

P50_VIEWS: tuple[str, ...] = ("4", "8", "13")
"""The list row's p50 totals: the first 4, 8 and 13 forecast weeks (`serving.views`)."""


def _target_row(target: str, candidate: TargetCandidateV1, currency: str | None) -> RosterTargetV1:
    bands = candidate.chosen_forecast
    if bands is None:
        totals: dict[str, str | None] = dict.fromkeys(P50_VIEWS)
    else:
        values = [Decimal(value) for value in bands.p50]
        totals = {view: stored_text(sum(values[: int(view)], Decimal(0))) for view in P50_VIEWS}
    return RosterTargetV1(
        chosen_model=candidate.chosen_model,
        withheld_reason=candidate.withheld_reason,
        p50_totals=totals,  # pyright: ignore[reportArgumentType]
        currency_code=currency if target in MONEY_TARGETS else None,
    )


def roster_entry(
    candidate: CandidateSeriesV1, title: str | None, *, title_truncated: bool
) -> RosterEntryV1:
    """Return the candidate's unranked list row, with its display title already capped."""
    return RosterEntryV1(
        schema_version=1,
        rank=None,
        display_asin=candidate.display_asin,
        title=title,
        title_truncated=title_truncated,
        targets={  # pyright: ignore[reportArgumentType]
            target: _target_row(
                target,
                candidate.targets[target],  # pyright: ignore[reportArgumentType]
                candidate.currency_code,
            )
            for target in TARGETS
        },
        accuracy_class=None,
        stockout_weeks_last_13=candidate.stockout_weeks_last_13,
    )
