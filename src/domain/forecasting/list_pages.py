# SPDX-License-Identifier: MIT-0
"""The generation's global ranking and its precomputed list pages (design §6b, R5-03, R5-04).

`commit_generation` merges the shards' roster parts, ranks every entry once and cuts each list
filter into pages of `page_rows`, so a list request reads `META` and at most one page item at
any catalogue size. Callers may rely on:

- the filter set is closed: `all`, `shown`, and `withheld:<SkipReason>` for every declared
  reason, and every one of them is in `ListPages.index`, empty ones included (R5C2-10). An empty
  filter is `{entry_count: 0, page_count: 1}` with no page;
- a page holds at most `page_rows` entries in global rank order, and only the last page of a
  filter is short.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from domain.forecasting.boundary import RosterEntryV1
from domain.forecasting.models import SkipReason
from domain.forecasting.policy import ServingPolicy

ALL = "all"
SHOWN = "shown"
WITHHELD_PREFIX = "withheld:"
CLOSED_FILTERS: tuple[str, ...] = (
    ALL,
    SHOWN,
    *(f"{WITHHELD_PREFIX}{reason.value}" for reason in SkipReason),
)
"""Every list filter a request may name, in `META.pages` order."""
RANK_BY_UNITS_ORDERED_13W = "units_ordered_p50_13w"
_RANK_TARGET, _RANK_VIEW = "units_ordered", "13"


def filter_of(entry: RosterEntryV1, status_target: str) -> str:
    """Return the entry's status filter, read from `serving.status_target` (C-6)."""
    row = entry.targets[status_target]  # pyright: ignore[reportArgumentType]
    if row.chosen_model is not None:
        return SHOWN
    if row.withheld_reason is None:
        raise ValueError(f"{entry.display_asin}: {status_target} is neither shown nor withheld")
    return f"{WITHHELD_PREFIX}{row.withheld_reason.value}"


def merge_roster_parts(parts: Sequence[Sequence[RosterEntryV1]]) -> tuple[RosterEntryV1, ...]:
    """Concatenate the shards' unranked roster parts, in the order given."""
    return tuple(entry for part in parts for entry in part)


def _rank_key(entry: RosterEntryV1) -> tuple[int, Decimal, str]:
    row = entry.targets[_RANK_TARGET]
    if row.chosen_model is None:
        return (1, Decimal(0), entry.display_asin)
    total = row.p50_totals[_RANK_VIEW]
    if total is None:
        raise ValueError(f"{entry.display_asin}: a shown {_RANK_TARGET} row has no 13-week p50")
    return (0, -Decimal(total), entry.display_asin)


def rank_entries(entries: Sequence[RosterEntryV1], rank_by: str) -> tuple[RosterEntryV1, ...]:
    """Sort by the 13-week ordered-units p50 total, descending, then display ASIN; rank 1 to N.

    A series whose ordered-units target is withheld sorts after every shown one (§6b, R2-19).
    """
    if rank_by != RANK_BY_UNITS_ORDERED_13W:
        raise ValueError(f"serving.rank_by must be {RANK_BY_UNITS_ORDERED_13W}, got {rank_by!r}")
    ordered = sorted(entries, key=_rank_key)
    return tuple(
        entry.model_copy(update={"rank": rank}) for rank, entry in enumerate(ordered, start=1)
    )


@dataclass(frozen=True)
class ListPage:
    """One `PAGE#` item: a filter's page (from 1) of ranked entries."""

    filter: str
    page: int
    entries: tuple[RosterEntryV1, ...]


@dataclass(frozen=True)
class FilterPages:
    """`META.pages[<filter>]`."""

    page_count: int
    entry_count: int


@dataclass(frozen=True)
class ListPages:
    """Every non-empty page, and every closed filter's counts."""

    pages: tuple[ListPage, ...]
    index: dict[str, FilterPages]

    @property
    def first_page(self) -> tuple[RosterEntryV1, ...]:
        """`META.first_page`: page 1 of `all` (empty for an empty generation)."""
        return next((page.entries for page in self.pages if page.filter == ALL), ())


def cut_pages(ranked: Sequence[RosterEntryV1], serving: ServingPolicy) -> ListPages:
    """Cut `all` and each entry's status filter into pages of `serving.page_rows`."""
    buckets: dict[str, list[RosterEntryV1]] = {name: [] for name in CLOSED_FILTERS}
    previous = 0
    for entry in ranked:
        if entry.rank is None or entry.rank <= previous:
            raise ValueError(f"{entry.display_asin}: entries must be ranked, in rank order")
        previous = entry.rank
        buckets[ALL].append(entry)
        buckets[filter_of(entry, serving.status_target)].append(entry)
    rows = serving.page_rows
    pages: list[ListPage] = []
    index: dict[str, FilterPages] = {}
    for name, entries in buckets.items():
        count = len(entries)
        page_count = max(1, -(-count // rows))
        index[name] = FilterPages(page_count=page_count, entry_count=count)
        pages.extend(
            ListPage(name, number, tuple(entries[(number - 1) * rows : number * rows]))
            for number in range(1, page_count + 1)
            if count
        )
    return ListPages(tuple(pages), index)
